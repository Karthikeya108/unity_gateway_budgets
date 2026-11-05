-- Project-based budgets with RBAC roles: system-table queries.
--
-- Under an assumed role the gateway records the ROLE (account group) as the requester and the
-- budget applies the role's override. The human behind a role call is only in the audit log
-- (identity_metadata.run_by). Run these in the SQL editor; the :named parameters become widgets.
--
--   :project_group   account group backing the project role, e.g. 'project_alpha'
--   :service_name    model service FQN, e.g. 'my_catalog.shared.project_model_service'
--   :start_ts        window start, e.g. '2026-10-01 00:00:00'
--
-- system.ai_gateway.usage trails real time by ~20 min to a few hours; system.billing.usage by
-- several hours. The budget's own enforcement is faster than both.


-- 1. Project spend, month to date, per role (estimated USD from tokens x price map).
--    Databricks-hosted pay-per-token models have no per-user USD table, so spend is derived from
--    tokens. Keep the price map in sync with config/tiers.yaml (USD per 1M tokens).
SELECT
  requester                                          AS project_role,
  destination_model,
  COUNT(*)                                           AS calls,
  SUM(input_tokens)                                  AS in_tok,
  SUM(output_tokens)                                 AS out_tok,
  ROUND(
    SUM(input_tokens) / 1e6 *
      CASE WHEN destination_model ILIKE '%opus%'   THEN 8.20
           WHEN destination_model ILIKE '%sonnet%' THEN 3.27
           WHEN destination_model ILIKE '%haiku%'  THEN 1.63
           ELSE 8.20 END
    + SUM(output_tokens) / 1e6 *
      CASE WHEN destination_model ILIKE '%opus%'   THEN 41.00
           WHEN destination_model ILIKE '%sonnet%' THEN 16.34
           WHEN destination_model ILIKE '%haiku%'  THEN 8.17
           ELSE 41.00 END
  , 4)                                               AS est_usd
FROM system.ai_gateway.usage
WHERE requester_type = 'USER_GROUP'
  AND event_time >= date_trunc('month', current_timestamp())
GROUP BY ALL
ORDER BY project_role, est_usd DESC;


-- 2. Request-level log for one project role (who / what / when / status).
--    requester = the role; the human is NOT in this table (see query 4).
SELECT
  event_time, requester, requester_type, service_name, service_tags,
  destination_model, api_type, status_code,
  input_tokens, output_tokens, total_tokens,
  request_tags, user_agent, ip_address, request_id
FROM system.ai_gateway.usage
WHERE requester = :project_group
  AND event_time >= :start_ts
ORDER BY event_time DESC;


-- 3. Personal vs project usage on a service: one row per requester type.
--    USER_GROUP rows are project (role) usage; the rest is personal usage.
SELECT
  requester_type,
  requester,
  COUNT(*)            AS calls,
  SUM(total_tokens)   AS tokens,
  MIN(event_time)     AS first_call,
  MAX(event_time)     AS last_call
FROM system.ai_gateway.usage
WHERE service_name = :service_name
  AND event_time >= :start_ts
GROUP BY ALL
ORDER BY requester_type, tokens DESC;


-- 4. Who is acting under a project role (the human behind the role), from the audit log.
--    Unity Catalog events made under a role carry run_by (the person or SP) and run_as (the role).
--    The gateway's own token check (accounts.oidcTokenAuthorization) has run_as only, so it is
--    excluded here.
SELECT
  identity_metadata.run_as                AS project_role,
  identity_metadata.run_by                AS actual_principal,
  COUNT(*)                                AS events,
  COUNT_IF(action_name = 'authzEval')     AS gateway_authz_checks,
  MIN(event_time)                         AS first_seen,
  MAX(event_time)                         AS last_seen
FROM system.access.audit
WHERE identity_metadata.run_as = :project_group
  AND identity_metadata.run_by IS NOT NULL
  AND event_time >= :start_ts
GROUP BY ALL
ORDER BY events DESC;


-- 5. Gateway activity under a role, by action (token checks, authz checks, tag lookups).
--    listEntityTagAssignments on the model service is the gateway reading its tags (how budget
--    tag filters are matched); authzEval is the check on the model behind the service.
SELECT
  service_name, action_name,
  identity_metadata.run_as,
  COALESCE(identity_metadata.run_by, '<null>')  AS run_by,
  response.status_code,
  COUNT(*)                                      AS n,
  MIN(event_time)                               AS first_seen,
  MAX(event_time)                               AS last_seen
FROM system.access.audit
WHERE identity_metadata.run_as = :project_group
  AND event_time >= :start_ts
GROUP BY ALL
ORDER BY first_seen;


-- 6. Detail of the gateway's authz checks and tag lookups under a role.
--    request_params.principal on authzEval shows which identity the destination model was
--    checked against (observed: the human, not the role).
SELECT
  event_time, action_name,
  identity_metadata.run_as, identity_metadata.run_by,
  response.status_code,
  request_params.entity_name                  AS model_service_tags_read,
  request_params.principal                    AS authz_principal,
  substr(request_params.actions, 1, 300)      AS actions
FROM system.access.audit
WHERE identity_metadata.run_as = :project_group
  AND action_name IN ('authzEval', 'listEntityTagAssignments')
  AND event_time >= :start_ts
ORDER BY event_time;


-- 7. Approximate per-person split of a project's usage.
--    There is no shared request id between usage and audit, so each usage row is matched to the
--    nearest authzEval by the same role within a few seconds. Treat it as an estimate.
WITH u AS (
  SELECT request_id, event_time, requester, total_tokens
  FROM system.ai_gateway.usage
  WHERE requester = :project_group AND event_time >= :start_ts
),
a AS (
  SELECT event_time, identity_metadata.run_as AS run_as, identity_metadata.run_by AS run_by
  FROM system.access.audit
  WHERE action_name = 'authzEval'
    AND identity_metadata.run_as = :project_group
    AND event_time >= :start_ts
),
matched AS (
  SELECT u.request_id, u.total_tokens, a.run_by,
         ROW_NUMBER() OVER (PARTITION BY u.request_id
                            ORDER BY abs(unix_millis(u.event_time) - unix_millis(a.event_time))) AS rn
  FROM u JOIN a
    ON a.run_as = u.requester
   AND a.event_time BETWEEN u.event_time - INTERVAL 10 SECONDS AND u.event_time + INTERVAL 10 SECONDS
)
SELECT run_by AS actual_principal, COUNT(*) AS calls, SUM(total_tokens) AS tokens
FROM matched
WHERE rn = 1
GROUP BY ALL
ORDER BY tokens DESC;


-- 8. Billing (DBUs) for gateway usage, by run_as / run_by.
--    Not yet verified against live rows: confirms whether billing attributes role usage to the
--    role (run_as) and still keeps the human (run_by). If it returns nothing, drop the
--    ai_gateway IS NOT NULL filter and inspect usage_metadata.
SELECT
  usage_date,
  identity_metadata.run_as,
  identity_metadata.run_by,
  usage_metadata.ai_gateway,
  custom_tags,
  sku_name,
  ROUND(SUM(usage_quantity), 4)  AS dbus,
  usage_unit
FROM system.billing.usage
WHERE usage_date >= to_date(:start_ts)
  AND usage_metadata.ai_gateway IS NOT NULL
GROUP BY ALL
ORDER BY usage_date DESC, dbus DESC;


-- 9. Failed calls for a role (budget blocks, access errors).
--    Not yet verified that 403 budget blocks are logged here; 400 "Invoker principal not found"
--    calls were NOT logged in either usage or audit.
SELECT event_time, requester, service_name, status_code, api_type, request_id
FROM system.ai_gateway.usage
WHERE requester = :project_group
  AND status_code >= 400
  AND event_time >= :start_ts
ORDER BY event_time DESC;
