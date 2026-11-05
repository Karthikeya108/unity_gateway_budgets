-- Delta tables backing the approval queue and the tier-change audit log.
-- Substitute {{catalog}} and {{schema}} to match config/tiers.yaml.

CREATE SCHEMA IF NOT EXISTS {{catalog}}.{{schema}};

CREATE TABLE IF NOT EXISTS {{catalog}}.{{schema}}.approvals (
  request_id    STRING    NOT NULL,
  principal     STRING    NOT NULL,   -- user email / SP id requesting the increase
  from_tier     STRING    NOT NULL,
  to_tier       STRING    NOT NULL,
  spend_usd     DOUBLE,               -- verified month-to-date paid spend at request time
  status        STRING    NOT NULL,   -- pending | approved | denied
  approver      STRING,               -- verified approver principal
  reason        STRING,
  requested_at  TIMESTAMP NOT NULL,
  decided_at    TIMESTAMP
) USING DELTA;

CREATE TABLE IF NOT EXISTS {{catalog}}.{{schema}}.tier_audit (
  principal          STRING    NOT NULL,
  old_tier           STRING,
  new_tier           STRING    NOT NULL,   -- may be 'base' when dropped to the base pool pending approval
  trigger            STRING    NOT NULL,   -- alert | reconcile | approval
  verified_spend_usd DOUBLE,
  actor              STRING    NOT NULL,   -- 'system' or the approver principal
  changed_at         TIMESTAMP NOT NULL
) USING DELTA;

-- Assigned entitlement per user: the tier they are ENTITLED to, independent of
-- current group membership. Group membership is the live enforcement (access to
-- a tier's endpoint pool); this table is the source of truth the reconcile job
-- converges membership toward. A user over-threshold with a pending approval is
-- entitled to their current tier but temporarily dropped to the base pool.
CREATE TABLE IF NOT EXISTS {{catalog}}.{{schema}}.user_tier (
  principal    STRING    NOT NULL,
  tier         STRING    NOT NULL,   -- entitled tier name (e.g. tier-1/2/3)
  state        STRING    NOT NULL,   -- active | pending_upgrade | over_pending
  updated_at   TIMESTAMP NOT NULL
) USING DELTA;

-- In-app notification inbox (shown in the app; also mirrored to Slack when configured).
CREATE TABLE IF NOT EXISTS {{catalog}}.{{schema}}.notifications (
  id           STRING    NOT NULL,
  principal    STRING    NOT NULL,   -- recipient (user email)
  kind         STRING    NOT NULL,   -- budget_hit | request_created | approved | denied
  title        STRING,
  body         STRING,
  request_id   STRING,               -- linked approval request, when applicable
  created_at   TIMESTAMP NOT NULL,
  read         BOOLEAN
) USING DELTA;
