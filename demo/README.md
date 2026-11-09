# Demo runbook: AI Budget Center (user-initiated elevation)

An end-to-end walkthrough of the app. A user hits their AI budget, gets notified with their
usage and a link, requests more budget in the app, and a steward approves. The user is
notified and their ceiling is raised. One app, two role-aware views.

Model: one shared endpoint (`common_tier_model_service`) plus one budget with per-group
overrides (default $1, override $2 for `power_users`, $4 for `super_users`). Elevation adds
the user to the next override group; the highest override applies.

Replace the placeholders below with your own workspace host, app URL, profile, warehouse id,
and group names.

## What the audience sees

1. User view: the user's spend vs their $1 ceiling (KPI tiles and a progress bar), usage by
   model, an empty notification inbox.
2. A call to the shared endpoint is blocked (budget exhausted). The client reports it and the
   user gets a notification ("You've reached your $1 AI budget") with their usage and a link.
   Work continues on the base model.
3. The user clicks Request more budget, which creates a request (tier-1 to tier-2).
4. Approver view (a steward): the pending request appears with the requester's month-to-date
   spend and top models. The steward clicks Approve.
5. The user gets an approved notification and their ceiling is now $2, so the next call
   succeeds.

This assumes the shared endpoint's month-to-date spend is already over $1, so the block fires
right away.

## Prerequisites (once)

```bash
export P=<your-profile>
export DATABRICKS_HOST=https://<your-workspace-host>
export APP_URL=https://<your-app-url>
export PRINCIPAL=<your-email>
export COMMON_SERVICE=<catalog>.<schema>.<shared-service>   # budgeted, tagged service
export BASE_SERVICE=<catalog>.<schema>.<base-service>       # unbudgeted fallback
export DATABRICKS_TOKEN=$(databricks auth token -p $P | jq -r .access_token)
```

Demo users must be in the users group (workspace access with consumer entitlement, `EXECUTE` on
both services, `CAN_USE` on the app); see the Quick start in the top-level README. The tier groups
start empty. To start with the block firing, spend just over the tier-1 amount on the shared
service first (for example `SERVICE=$COMMON_SERVICE python demo/breach_tier.py`).

## The demo

Step 0. Open the app (`$APP_URL/`) as the user. You are at the $1 default and usage shows you
are already over. Approvers see an extra Approvals tab.

Step 1. Trigger a block (the client reports it, then you get notified):

```bash
python demo/elevating_client.py "In one sentence, what is Unity Catalog?"
# BLOCKED: budget ceiling reached. Reporting to the app.
#   notified <email>: MTD ~$X at the $1 ceiling across N calls.
#   Open $APP_URL/ to request more budget (an approver will review it).
#   falling back to the base model so work continues. base OK: "Unity Catalog is ..."
```

Refresh the app. A notification appears and the banner shows you are over budget.

Step 2. Request elevation: click Request more budget in the user view, or
`curl -X POST $APP_URL/api/request-elevation -H "Authorization: Bearer $DATABRICKS_TOKEN"`.
To approve through the API instead of the UI, send `{"decision": "approved"}` to
`POST $APP_URL/api/approvals/<request-id>`. Any other value is treated as a denial.

Step 3. Approve: as a steward, open the Approvals tab, review the request (it shows MTD spend
and top models), and click Approve. The user gets an approved notification.

Step 4. Show the ceiling is raised (the new override takes about 30 seconds to apply):

```bash
python demo/elevating_client.py "Give me two sentences on Delta Lake."
# OK: succeeds at the $2 ceiling
```

Step 5. Governance trail:

```bash
databricks api post /api/2.0/sql/statements --json '{"warehouse_id":"<sql-warehouse-id>","wait_timeout":"50s",
 "statement":"SELECT changed_at, old_tier, new_tier, trigger, actor FROM internal_ai_services.budget_override.tier_audit ORDER BY changed_at DESC LIMIT 6"}' -p $P | jq -r '.result.data_array[][]'
```

This shows `tier-1 -> tier-1 (block, system)`, `tier-1 -> tier-1 (request, <user>)`, and
`tier-1 -> tier-2 (approval, <approver>)`.

## Reset between runs

Remove the user from the override groups (back to the $1 default) and clear kit state
(approvals, entitlement, notifications). The endpoint spend stays, so the block keeps firing:

```bash
CID=$(databricks secrets get-secret uag-budget account_sp_client_id -p $P | jq -r .value | base64 -d)
SEC=$(databricks secrets get-secret uag-budget account_sp_client_secret -p $P | jq -r .value | base64 -d)
env -u DATABRICKS_TOKEN -u DATABRICKS_HOST ACCT_CID=$CID ACCT_SEC=$SEC python - <<'PY'
import os
from app.budget.groups import GroupManager
from databricks.sdk import AccountClient
a = AccountClient(host="https://accounts.azuredatabricks.net", account_id="<account-id>",
                  client_id=os.environ["ACCT_CID"], client_secret=os.environ["ACCT_SEC"])
g = GroupManager(["power_users", "super_users"], client=a)
me = "<your-email>"
for grp in ["power_users", "super_users"]:
    if me in g.members(grp): g.remove(me, grp)
print("reset to default $1")
PY
for t in approvals user_tier notifications; do
  databricks api post /api/2.0/sql/statements --json "{\"warehouse_id\":\"<sql-warehouse-id>\",\"wait_timeout\":\"50s\",\"statement\":\"DELETE FROM internal_ai_services.budget_override.$t\"}" -p $P >/dev/null
done
```

## Notes

- Notifications: the in-app inbox is always on. Slack is an optional, admin-gated mirror. It
  posts only when a bot token (`slack_bot_token` in the `uag-budget` scope or `SLACK_BOT_TOKEN`)
  and channels (`SLACK_CHANNEL_USER`, `SLACK_CHANNEL_STEWARD`) are set; otherwise Slack is
  logged and the in-app inbox carries the demo. Enabling Slack needs admin help (a Slack app or
  bot token, or a Workflow Builder webhook trigger). The managed `system.ai.slack` MCP is
  read-only, so it cannot send.
- Two triggers: the client's 403 report is real time; the reconcile job
  (`uag-budget-reconcile-common`, paused for demo determinism) is a backstop that notifies
  over-budget users and clears stale state.
- `/api/events/budget-hit` prefers the verified `X-Forwarded-Email` and accepts a `principal`
  in the payload as a fallback. Elevation is always steward-approved.
