"""Per-user OpenAI-compatible multimodal model configurations.

API keys are encrypted at rest with a server-local Fernet key. Public payloads
never contain either the encrypted value or its plaintext form.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from .db import DATA_DIR

KEY_PREFIX = "custom:"
_KEY_FILE = DATA_DIR / ".custom-model.key"
_MODEL_KEY_RE = re.compile(r"^custom:([1-9][0-9]*)$")


def key_for(model_id: int) -> str:
    return f"{KEY_PREFIX}{int(model_id)}"


def id_from_key(value: str | None) -> int | None:
    match = _MODEL_KEY_RE.fullmatch(str(value or ""))
    return int(match.group(1)) if match else None


def _fernet() -> Fernet:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        key = _KEY_FILE.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = _KEY_FILE.read_bytes().strip()
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key + b"\n")
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    return Fernet(key)


def encrypt_api_key(value: str) -> str:
    value = str(value or "").strip()
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_api_key(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("自定义模型 API Key 无法解密，请删除后重新添加") from exc


def normalize_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Base URL 必须是完整的 http:// 或 https:// 地址")
    if parts.username or parts.password or parts.fragment:
        raise ValueError("Base URL 不能包含账号密码或锚点")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, ""))


def list_rows(con, user_id: int, *, active_only: bool = True):
    sql = "SELECT * FROM custom_models WHERE user_id = ?"
    params: list[object] = [user_id]
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY id"
    return con.execute(sql, params).fetchall()


def get_owned(con, user_id: int, model_key: str, *, active_only: bool = True):
    model_id = id_from_key(model_key)
    if model_id is None:
        return None
    sql = "SELECT * FROM custom_models WHERE id = ? AND user_id = ?"
    params: list[object] = [model_id, user_id]
    if active_only:
        sql += " AND is_active = 1"
    return con.execute(sql, params).fetchone()


def create(con, user_id: int, display_name: str, model_id: str, base_url: str, api_key: str):
    display_name = str(display_name or "").strip()
    model_id = str(model_id or "").strip()
    if not (1 <= len(display_name) <= 80):
        raise ValueError("模型名称需为 1–80 个字符")
    if not (1 <= len(model_id) <= 180):
        raise ValueError("Model ID 需为 1–180 个字符")
    base_url = normalize_base_url(base_url)
    if len(base_url) > 500:
        raise ValueError("Base URL 过长")
    if len(api_key or "") > 4096:
        raise ValueError("API Key 过长")
    now = int(time.time())
    cur = con.execute(
        "INSERT INTO custom_models(user_id,display_name,model_id,base_url,api_key_enc,"
        "vision_enabled,is_active,created_at,updated_at) VALUES(?,?,?,?,?,1,1,?,?)",
        (user_id, display_name, model_id, base_url, encrypt_api_key(api_key), now, now),
    )
    con.commit()
    return get_owned(con, user_id, key_for(cur.lastrowid))


def delete(con, user_id: int, model_key: str) -> bool:
    model_id = id_from_key(model_key)
    if model_id is None:
        return False
    cur = con.execute(
        "UPDATE custom_models SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
        (int(time.time()), model_id, user_id),
    )
    con.commit()
    return cur.rowcount > 0


def public_payload(row) -> dict:
    return {
        "key": key_for(row["id"]),
        "name": row["display_name"],
        "model_id": row["model_id"],
        "base_url": row["base_url"],
        "has_api_key": bool(row["api_key_enc"]),
        "multimodal": bool(row["vision_enabled"]),
    }


def runtime_config(row) -> dict:
    return {
        "label": row["display_name"],
        "backend": "openai",
        "engine_model": row["model_id"],
        "base_url": row["base_url"],
        "api_key": decrypt_api_key(row["api_key_enc"]),
        "slide_concurrency": 4,
        "custom": True,
    }
