"""Tier membership management via ACCOUNT SCIM.

Tier groups are Unity Catalog account groups, so their membership can only be
managed through the account SCIM API (workspace SCIM refuses: "group can only be
managed in account"). This uses an account-scoped client authenticated as a
dedicated account SP with the group-manager role on the tier groups only.

Least privilege: the kit only ever touches the tier groups named in config; the
allowed-set guard refuses any group outside it.
"""
from __future__ import annotations


class GroupManager:
    def __init__(self, tier_group_names: list[str], client=None):
        # `client` is an AccountClient; built lazily so importing / app startup does
        # not require account credentials until a membership op actually runs.
        self._client = client
        self._allowed = {g for g in tier_group_names if g}   # guard: never touch groups outside the tier set
        self._cache: dict[str, str] = {}         # displayName -> group id

    @property
    def _a(self):
        if self._client is None:
            from app.core.account import account_client
            self._client = account_client()
        return self._client

    def _group_id(self, name: str) -> str:
        if name not in self._allowed:
            raise ValueError(f"refusing to manage group outside tier set: {name}")
        if name in self._cache:
            return self._cache[name]
        matches = list(self._a.groups.list(filter=f'displayName eq "{name}"'))
        if not matches:
            raise LookupError(f"tier group not found (operator must create it): {name}")
        self._cache[name] = matches[0].id
        return matches[0].id

    def members(self, name: str) -> set[str]:
        """Return member principals as userName/email (matches usage.requester).

        Empty name (e.g. a default tier with no override group) -> no members.

        SCIM member.display is the display name ("Jane Doe"), but the system tables
        key spend on userName (email). Resolve each member id to its userName; fall
        back to display if resolution fails (e.g. SP members).
        """
        if not name:
            return set()
        gid = self._group_id(name)
        g = self._a.groups.get(gid)
        out: set[str] = set()
        for m in (g.members or []):
            principal = None
            if m.value:
                try:
                    principal = self._a.users.get(m.value).user_name
                except Exception:
                    principal = None
            out.add(principal or m.display)
        return out

    def is_member(self, group_name: str, principal: str) -> bool:
        """Read-only: is `principal` (email) a member of `group_name`?

        Used to authorize approvers against the stewards group. Not write-guarded
        (the allowed-set guard only protects membership *writes*); resolves member
        ids to userName to compare against the caller's verified email.
        """
        matches = list(self._a.groups.list(filter=f'displayName eq "{group_name}"'))
        if not matches:
            return False
        g = self._a.groups.get(matches[0].id)
        for m in (g.members or []):
            if m.display == principal:
                return True
            if m.value:
                try:
                    if self._a.users.get(m.value).user_name == principal:
                        return True
                except Exception:
                    pass
        return False

    def move(self, principal: str, from_group: str | None, to_group: str) -> None:
        """Add principal to to_group and remove from from_group (both in the tier set)."""
        self._add(principal, to_group)
        if from_group and from_group != to_group:
            self._remove(principal, from_group)

    def add(self, principal: str, group: str) -> None:
        """Grant a tier pool by adding the principal to its group."""
        self._add(principal, group)

    def remove(self, principal: str, group: str) -> None:
        """Revoke a tier pool by removing the principal from its group (drops to base)."""
        self._remove(principal, group)

    def _principal_id(self, principal: str) -> str:
        users = list(self._a.users.list(filter=f'userName eq "{principal}"'))
        if users:
            return users[0].id
        sps = list(self._a.service_principals.list(filter=f'displayName eq "{principal}"'))
        if sps:
            return sps[0].id
        raise LookupError(f"principal not found: {principal}")

    def _add(self, principal: str, group: str) -> None:
        from databricks.sdk.service import iam
        gid = self._group_id(group)
        pid = self._principal_id(principal)
        self._a.groups.patch(
            gid,
            operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                  value=[{"value": pid}])],
            schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        )

    def _remove(self, principal: str, group: str) -> None:
        from databricks.sdk.service import iam
        gid = self._group_id(group)
        pid = self._principal_id(principal)
        self._a.groups.patch(
            gid,
            operations=[iam.Patch(op=iam.PatchOp.REMOVE, path=f'members[value eq "{pid}"]')],
            schemas=[iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP],
        )
