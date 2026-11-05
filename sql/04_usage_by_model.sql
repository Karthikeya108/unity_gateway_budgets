-- Query backing the app's live "usage by model" view (app/core/systables.py :: usage_breakdown).
-- Per-user, month-to-date usage on the shared common endpoint, with estimated $ from the
-- calibrated price map (in-geo Opus $8.20/$41.00 per 1M; ~$0.104/DBU). Change the requester.
SELECT
  destination_model,
  COUNT(*)                                          AS calls,
  COALESCE(SUM(input_tokens), 0)                    AS in_tok,
  COALESCE(SUM(output_tokens), 0)                   AS out_tok,
  ROUND(
    COALESCE(SUM(input_tokens), 0) / 1e6 *
      CASE WHEN destination_model ILIKE '%opus%'   THEN 8.20
           WHEN destination_model ILIKE '%sonnet%' THEN 3.27
           WHEN destination_model ILIKE '%haiku%'  THEN 1.63
           ELSE 8.20 END
    + COALESCE(SUM(output_tokens), 0) / 1e6 *
      CASE WHEN destination_model ILIKE '%opus%'   THEN 41.00
           WHEN destination_model ILIKE '%sonnet%' THEN 16.34
           WHEN destination_model ILIKE '%haiku%'  THEN 8.17
           ELSE 41.00 END
  , 4)                                              AS est_usd
FROM system.ai_gateway.usage
WHERE requester = 'user@example.com'   -- set to the principal you want to inspect
  AND event_time >= date_trunc('month', current_timestamp())
  AND service_name IN ('internal_ai_services.shared.common_tier_model_service')
GROUP BY destination_model
ORDER BY out_tok DESC;
