"""Auth helpers: stdlib password hashing (scrypt) + server-side sessions.

Open self-registration: users pick a username + password (no email). The first
admin can still be created via `app.cli create-admin`.
"""
import hashlib
import os
import re
import secrets
import sqlite3
import time

SESSION_COOKIE = os.environ.get("STUDIO_SESSION_COOKIE", "studio_session")
SESSION_TTL = 14 * 24 * 3600   # 14 days

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
# No password length/complexity rules — any non-empty password is accepted.

# scrypt parameters (memory ~16MB, well under the 32MB default maxmem cap)
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 32


class UsernameTaken(Exception):
    pass


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt$%s$%s" % (salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def create_user(con, username: str, password: str, role: str = "user", display_name: str = "") -> int:
    """Insert a user. Raises UsernameTaken on duplicate username."""
    now = int(time.time())
    try:
        cur = con.execute(
            "INSERT INTO users(username,password_hash,display_name,role,is_active,created_at) "
            "VALUES(?,?,?,?,1,?)",
            (username, hash_password(password), display_name.strip() or username, role, now),
        )
    except sqlite3.IntegrityError as e:
        raise UsernameTaken(username) from e
    con.commit()
    return cur.lastrowid


def create_session(con, user_id: int, ip: str = "", ua: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    con.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at,ip,user_agent) VALUES(?,?,?,?,?,?)",
        (token, user_id, now, now + SESSION_TTL, ip, ua),
    )
    con.commit()
    return token


def set_session_cookie(resp, token: str) -> None:
    # Secure flag omitted for local http dev; enable behind TLS in production.
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
    )


def user_from_request(request, con):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, int(time.time())),
    ).fetchone()
    if row and row["is_active"]:
        return row
    return None
