"""Account-scoped client for managing account-group membership.

Tier groups are Unity Catalog account groups (UC grants only recognize account-level
principals), so their membership can only be changed via the ACCOUNT SCIM API - not
workspace SCIM. This builds an AccountClient authenticated as a dedicated account
service principal that has the "group manager" role on the tier groups only.

Credentials are resolved lazily from environment (never hardcoded):
  ACCOUNT_HOST            account console host (default: https://accounts.azuredatabricks.net)
  DATABRICKS_ACCOUNT_ID   the account id
  ACCOUNT_SP_CLIENT_ID    dedicated account SP application id      (or via secret scope)
  ACCOUNT_SP_CLIENT_SECRET  its OAuth secret                        (or via secret scope)

If the client id/secret are not in the environment but ACCOUNT_SP_SECRET_SCOPE is set,
they are read from that Databricks secret scope (keys: account_sp_client_id /
account_sp_client_secret) using the ambient WorkspaceClient identity.
"""
from __future__ import annotations

import os
from functools import lru_cache

from databricks.sdk import AccountClient


def _creds() -> tuple[str, str]:
    cid = os.environ.get("ACCOUNT_SP_CLIENT_ID")
    sec = os.environ.get("ACCOUNT_SP_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    scope = os.environ.get("ACCOUNT_SP_SECRET_SCOPE")
    if scope:
        import base64
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()

        def _get(key: str) -> str:
            resp = w.secrets.get_secret(scope, key)
            return base64.b64decode(resp.value).decode()

        cid = _get(os.environ.get("ACCOUNT_SP_CLIENT_ID_KEY", "account_sp_client_id"))
        sec = _get(os.environ.get("ACCOUNT_SP_CLIENT_SECRET_KEY", "account_sp_client_secret"))
        return cid, sec
    raise RuntimeError(
        "account SP credentials not configured: set ACCOUNT_SP_CLIENT_ID/ACCOUNT_SP_CLIENT_SECRET "
        "or ACCOUNT_SP_SECRET_SCOPE"
    )


@lru_cache(maxsize=1)
def account_client() -> AccountClient:
    host = os.environ.get("ACCOUNT_HOST", "https://accounts.azuredatabricks.net")
    account_id = os.environ["DATABRICKS_ACCOUNT_ID"]
    client_id, client_secret = _creds()
    return AccountClient(
        host=host, account_id=account_id,
        client_id=client_id, client_secret=client_secret,
    )
