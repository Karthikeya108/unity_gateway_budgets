# Project-Based AI Budgets with Unity AI Gateway and RBAC Roles

## The problem

Unity AI Gateway budgets are per user by default. A user's month-to-date spend follows them
everywhere, and putting them in a higher override group only raises their personal ceiling. It
does not give them a new allowance. So you can't say "this spend belongs to Project A and comes
out of Project A's budget."

## The idea

Make each project a **role**. In Databricks RBAC a role is backed by an account group. A user
assumes the role for a session, and while they hold it they act **as the role**: the role's
permissions replace their own.

Unity AI Gateway treats the assumed role as the caller:

- The budget applies the **role's** threshold, not the user's.
- Spend under the role counts toward the **role's** total, and **everyone** who assumes the role
  draws from that same pool.
- The same person can have several separate allowances: their own, Project A's, and Project B's.
  Which one applies depends only on the role they assume.

<!-- DIAGRAM: user -> assume role (project group) -> Unity AI Gateway model service -> budget override for the project group -->

## How it works

| Piece | What it is |
|---|---|
| Project role | An **account group** per project, for example `project_alpha` |
| Who can use it | Users and service principals given the **Assume** permission on the group. They should **not** be members of it (see the rules below) |
| Access | The group is granted `USE CATALOG`, `USE SCHEMA` and `EXECUTE` on the model service |
| Project budget | A Unity AI Gateway budget scoped by a model-service tag, with a **per-user override for the project group** set to the project's monthly allowance |
| Personal budget | The budget's per-user default threshold. It applies whenever someone calls **without** a role |
| Enforcement | The budget's native block. When the project's pool is used up, every caller in that role gets HTTP 403 `Budget "<name>" has reached its limit of $<project amount>` |

## Setup

1. **Enable RBAC:** an account admin turns on the RBAC preview, then a workspace admin turns it on
   for the workspace.
2. **Create the project role:** create an account group per project and assign it to the workspace.
3. **Let people assume it:** on the group's Permissions page, grant **Assume**
   (`roles/group.assumer`) to the project's users, an existing team group, or service principals.
4. **Grant model access to the role:** on the catalog, schema and model service, grant
   `USE CATALOG`, `USE SCHEMA` and `EXECUTE` to the project group.
5. **Tag the model service:** for example `budget_scope=projects`. Budget filters match
   **model-service tags**; request tags don't count.
6. **Create the budget:** resource type Unity AI Gateway, filtered on that tag:
   - Per-user default: the personal allowance outside any project (it can be `$0`).
   - Per-user override per project group: the project's monthly allowance, with Block on.
   - Optional shared threshold: a ceiling across all projects.

## How users call it

- **CLI or SDK (as the user):** run `databricks auth login --host <workspace> -p <profile>`, pick
  the role in the browser, then use that profile's token as normal. The token carries the group in
  its `ag` claim.
- **Service principal (M2M):** add `assume_group=<group-id>` to the workspace token request:
  `POST <workspace>/oidc/v1/token` with `grant_type=client_credentials`, `scope=all-apis` and
  `assume_group=<group-id>`. This works only for workspace-level tokens.
- **Gateway call:** unchanged, for example
  `POST <workspace>/ai-gateway/mlflow/v1/chat/completions` with `"model": "<catalog>.<schema>.<model-service>"`.

## Tracking spend

| Question | Where to look |
|---|---|
| Project spend | `system.ai_gateway.usage`: `requester = '<project group>'` and `requester_type = 'USER_GROUP'` |
| Who spent it | `system.access.audit`: Unity Catalog events with `identity_metadata.run_as = '<project group>'`. `identity_metadata.run_by` is the actual person |
| Live budget status | Account console, Budgets, the budget, **Per user spend** tab. The project group shows up as a "user" |

The usage table records **only the role**, not the person. To break a project's spend down by
person, join it to the audit log's `run_by`.

## Rules and caveats

- **Assume, don't join.** If a user is a **member** of the project group, the project's override
  becomes their personal ceiling as well (the highest override applies), and their own spend no
  longer stays separate from the project's. Give project users Assume through a separate group,
  not membership.
- **One role per session.** A user picks the project at login. Moving to another project means a
  different profile or token.
- **Access goes only through the role.** While a user holds a role they lose their own
  permissions, and someone with no direct grant can reach the service only through the role.
- **Grants take a few minutes to apply.** A new grant to a role can return 404 "does not exist"
  until it propagates.
- **Budgets are near-real-time, not hard caps.** A small overspend can get through, and requests
  already running are not stopped.
- **Limits:** a budget can have at most 20 per-user overrides (about 20 projects per budget). An
  account can have at most 1,000 budgets.
- **Check each model service with a role first.** In testing, a model service with a guardrail
  policy, rate limits and an inference table rejected role callers with `400 Invoker principal
  not found: <group>`, while a plain routing-only service worked. Make a test call as a role on
  every service before rolling it out.

## Validated behaviour

| Caller | Role assumed | Result |
|---|---|---|
| User (not a member of either project group) | none | 403 at the `$0` personal default |
| Same user | Project A (override `$0.01`, exhausted) | 403 at `$0.01` |
| Same user | Project B (override `$2`) | 200 |
| Service principal with no spend of its own | Project A | **403 at `$0.01`**: the pool is shared with everyone in the role |
| Same service principal | Project B | 200 |
