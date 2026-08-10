"""Runtime product profile switches for SenseNova Present Studio.

The V1 profile intentionally hides product surfaces instead of deleting their
implementation.  This keeps dynamic presentations and account-aware code
available for a later release while the current deployment operates as one
anonymous, static-only workspace.
"""
from __future__ import annotations

import os
import time


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


# V1 is the safe product default for this release.  Keeping the default here
# (rather than only in launcher scripts) prevents a bare ``uvicorn`` restart
# from accidentally exposing account authentication and Dynamic Present.
# The preserved full product can still be enabled explicitly with
# ``STUDIO_EDITION=full``.
EDITION = os.environ.get("STUDIO_EDITION", "v1").strip().lower() or "v1"
IS_V1 = EDITION in {"v1", "static", "static-v1"}
AUTH_ENABLED = _flag("STUDIO_AUTH_ENABLED", not IS_V1)
DYNAMIC_ENABLED = _flag("STUDIO_DYNAMIC_ENABLED", not IS_V1)
LANGUAGE = (
    os.environ.get("STUDIO_LANGUAGE", "zh").strip().lower()
    if os.environ.get("STUDIO_LANGUAGE", "zh").strip().lower() in {"zh", "en"}
    else "zh"
)
SINGLE_USER_USERNAME = (
    os.environ.get("STUDIO_SINGLE_USER_USERNAME", "user").strip() or "user"
)


def ensure_single_user(con):
    """Return the canonical V1 user, creating it if migration has not run yet."""
    row = con.execute(
        "SELECT * FROM users WHERE username = ?", (SINGLE_USER_USERNAME,)
    ).fetchone()
    if row is None:
        now = int(time.time())
        con.execute(
            "INSERT INTO users(username,password_hash,display_name,role,is_active,created_at) "
            "VALUES(?,?,?,?,1,?)",
            (SINGLE_USER_USERNAME, "disabled-v1", "user", "user", now),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM users WHERE username = ?", (SINGLE_USER_USERNAME,)
        ).fetchone()
    elif not row["is_active"]:
        con.execute("UPDATE users SET is_active = 1 WHERE id = ?", (row["id"],))
        con.commit()
        row = con.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
    return row


def user_for_request(request, con, auth_module):
    if AUTH_ENABLED:
        return auth_module.user_from_request(request, con)
    return ensure_single_user(con)


def public_payload() -> dict:
    return {
        "edition": EDITION,
        "auth_enabled": AUTH_ENABLED,
        "dynamic_enabled": DYNAMIC_ENABLED,
        "language": LANGUAGE,
        "single_user": not AUTH_ENABLED,
    }
