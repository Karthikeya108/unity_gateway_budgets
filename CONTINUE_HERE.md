# Continue Here: UAG Tiered Budget Elevation Kit

Short handoff for this directory.

## What this is

A generic, reusable tiered per-user budget and governed elevation kit for Unity AI Gateway,
built on per-group budget overrides: one shared model endpoint and one budget (a default
limit plus per-group overrides). "More budget" means being added to the next override group
(highest applies, no removal). Enforcement is the budget's native block; a base endpoint is
the fallback that keeps users working. Full design and as-built details are in `docs/TIERED_ELEVATION.md`;
demo steps are in `demo/README.md`. The project-budget (RBAC role) pattern is in
`docs/PROJECT_BUDGETS_RBAC.md`, with monitoring queries in `sql/03_project_budget_queries.sql`.

## Model

- User-initiated, approval-gated flow with a role-aware UI. A user hits the budget, gets
  notified in the app (Slack optional) with usage and a link, requests more in the app, a
  steward approves or denies, the user is notified, and the ceiling is raised (the user is
  added to the next override group).
- Two views (user and approver) served from `app/static/index.html`. JSON APIs under `/api/*`.
  Identity is `X-Forwarded-Email`; an approver is a member of the stewards group.
- Tier groups are account groups, so membership is managed through the account SCIM API with a
  dedicated account service principal (its client id and secret live in a secret scope).

## Config

Deployment-specific values (catalog, schema, group names, service names, thresholds,
account id, workspace host, app URL, warehouse id) live in `config/tiers.yaml` and `app.yaml`.
`config/tiers.yaml` is gitignored. See `config/tiers.example.yaml` for the template.

## Tests

```bash
TIERS_CONFIG=config/tiers.example.yaml ./.venv/bin/python tests/test_escalation.py
```

## Open or optional

- Harden `/api/events/budget-hit` to derive the principal only from `X-Forwarded-Email`
  (it currently accepts a payload principal as a fallback).
- Enable the reconcile job schedule if you want the backstop self-driving.
- Wire the optional Slack mirror when an admin can provide a bot token or a webhook trigger.
