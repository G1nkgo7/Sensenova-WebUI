"""pptagent_static_web_demo Studio — auth + workspace + deck generation jobs.

Run:  cd studio && uv run uvicorn app.main:app --host 0.0.0.0 --port 8011 --reload
"""
import asyncio
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import urllib.parse
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               JSONResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, SecretStr

# Keep source files, generated workspaces and exported artifacts readable by
# collaborators on the shared AFS mount.  This only removes the process-level
# write mask for group/other reads; explicit 0600 credential files remain
# private because their writers set that mode deliberately.
os.umask(0o022)

# Keep every generation backend on one explicit per-turn completion budget.
# This must run before importing ``dynamic`` because its runtime reads
# STUDIO_MAX_TOKENS at module import time.
_AGENT_MAX_TOKENS = os.environ.get("STUDIO_AGENT_MAX_TOKENS", "65536")
for _token_env in (
    "MAX_TOKENS",
    "SUBAGENT_MAX_TOKENS",
    "CLEAN_MAX_TOKENS",
    "STUDIO_MAX_TOKENS",
):
    os.environ[_token_env] = _AGENT_MAX_TOKENS

from . import (attachments, auth, custom_fonts, custom_models, dynamic, engine,
               features, jobs, service_config, titles, trace,
               trajectory_monitor)
from .db import DATA_DIR, connect, init_db

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _asset_ver() -> str:
    """Content-based CSS/JS cache key; independent of shared-volume mtimes."""
    static = BASE_DIR / "static"
    try:
        digest = hashlib.sha256()
        for name in ("app.css", "app.js"):
            digest.update(name.encode("utf-8"))
            digest.update((static / name).read_bytes())
        return digest.hexdigest()[:12]
    except OSError:
        return "0"


templates.env.globals["asset_ver"] = _asset_ver()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not features.AUTH_ENABLED:
        con = connect()
        try:
            features.ensure_single_user(con)
        finally:
            con.close()
    # Only the real ASGI service owns dynamic worker threads.  Import-time
    # reconciliation lets tests/utility scripts incorrectly interrupt jobs
    # that are alive in another process.
    if features.DYNAMIC_ENABLED:
        dynamic.runtime.reconcile_orphaned()
    await jobs.start()          # recover orphaned runs + spawn dispatcher pool
    yield


app = FastAPI(title="SenseNova-Present", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
if features.DYNAMIC_ENABLED:
    app.include_router(dynamic.router)


def get_db():
    con = connect()
    try:
        yield con
    finally:
        con.close()


def current_user(request: Request, con=Depends(get_db)):
    return features.user_for_request(request, con, auth)


def require_user(user=Depends(current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


# The trajectory monitor is a read-only projection of explicitly configured
# batch roots.  Keep it public so reviewers can open shared trajectory links
# without a Studio account; all mutating Studio routes remain authenticated.
app.include_router(trajectory_monitor.router)


def _client(request: Request):
    ip = request.client.host if request.client else ""
    return ip, request.headers.get("user-agent", "")


@app.get("/healthz")
def healthz():
    return {"ok": True, **features.public_payload()}


# ---- login / logout -------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user=Depends(current_user)):
    if not features.AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/" if user else "/?auth=login", status_code=303)


@app.post("/api/auth/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), con=Depends(get_db)):
    if not features.AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="V1 为免登录单用户模式")
    row = con.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (username.strip(),)
    ).fetchone()
    if not row or not auth.verify_password(password, row["password_hash"]):
        return JSONResponse({"ok": False, "error": "用户名或密码错误"}, status_code=401)
    ip, ua = _client(request)
    token = auth.create_session(con, row["id"], ip, ua)
    resp = JSONResponse({
        "ok": True,
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
    })
    auth.set_session_cookie(resp, token)
    return resp


@app.post("/api/auth/logout")
def logout(request: Request, con=Depends(get_db)):
    if not features.AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        con.execute("DELETE FROM sessions WHERE token = ?", (token,))
        con.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


# ---- open self-registration ----------------------------------------------
@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user=Depends(current_user)):
    if not features.AUTH_ENABLED:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/" if user else "/?auth=register", status_code=303)


@app.post("/api/auth/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    con=Depends(get_db),
):
    if not features.AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="V1 为免登录单用户模式")
    username = username.strip()

    def err(msg, code=400):
        return JSONResponse({"ok": False, "error": msg}, status_code=code)

    if not auth.USERNAME_RE.match(username):
        return err("用户名需 3-32 位,仅限字母 / 数字 / 下划线 / . -")
    if not password:
        return err("请输入密码")
    if password != password2:
        return err("两次输入的密码不一致")
    try:
        user_id = auth.create_user(con, username, password, role="user")
    except auth.UsernameTaken:
        return err("用户名已被占用")

    ip, ua = _client(request)
    token = auth.create_session(con, user_id, ip, ua)
    row = con.execute("SELECT username, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    resp = JSONResponse({
        "ok": True,
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
    })
    auth.set_session_cookie(resp, token)
    return resp


# ---- workspace shell ------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, user=Depends(current_user), con=Depends(get_db)):
    # Long-horizon lives in ppt-html-pipeline and can be updated independently
    # of this service. Refresh its readiness/revision for every new page load.
    engine.refresh_external_skills()
    authenticated = bool(user)
    view_user = user or {
        "id": 0, "username": "访客", "display_name": "访客", "role": "guest",
    }
    convos = [] if not authenticated else con.execute(
        "SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)
    ).fetchall()
    model_options = _model_options(con, user["id"] if authenticated else None)
    configured_default = os.environ.get("PPTAGENT_DEFAULT_MODEL", "").strip()
    available_model_keys = {item[0] for item in model_options}
    default_model = (
        configured_default if configured_default in available_model_keys
        else (model_options[0][0] if model_options else "")
    )
    return templates.TemplateResponse(
        request, "app.html", {
            "user": view_user, "authenticated": authenticated, "conversations": convos,
            "edition": features.EDITION,
            "auth_enabled": features.AUTH_ENABLED,
            "dynamic_enabled": features.DYNAMIC_ENABLED,
            "default_language": features.LANGUAGE,
            # 下拉模型选项直接由后端注册表生成,避免前端硬编码与 engine.MODELS 漂移
            # (漂移会让用户选到注册表里没有的 id → create_deck 报「未知模型」)。
            "models": model_options,
            "default_model": default_model,
            "pipelines": [(k, p["label"], ",".join(p["supports"]), p["skill_mode"],
                           ",".join(p.get("caps", [])), int(p.get("ready", True)))
                          for k, p in engine.PIPELINES.items()
                          if features.DYNAMIC_ENABLED or "dynamic_html" not in p.get("caps", [])],
            "default_pipeline": engine.DEFAULT_PIPELINE,
            "skills": [(k, engine.SKILLS[k]["label"], engine.SKILLS[k].get("pipeline", ""),
                        int(engine.SKILLS[k].get("ready", False)),
                        engine.SKILLS[k].get("unavailable_reason", ""))
                       for k in engine.PUBLIC_SKILL_KEYS],
            "default_skill": engine.DEFAULT_SKILL,
            "generation_stack": engine.default_generation_stack(
                default_model or engine.DEFAULT_MODEL
            ),
        }
    )


def _model_options(con, user_id: int | None):
    stack = engine.default_generation_stack(engine.DEFAULT_MODEL)
    options = [
        (key, engine.model_option_label(model), model["backend"],
         stack["pipeline"], stack["skill"],
         int(engine.thinking_capability(key)["toggle"]))
        for key, model in engine.user_selectable_models()
    ]
    if user_id is not None:
        options.extend(
            (custom_models.key_for(row["id"]), row["display_name"], "openai",
             stack["pipeline"], stack["skill"], 0)
            for row in custom_models.list_rows(con, user_id)
        )
    return options


def _model_label(con, user_id: int, model_key: str) -> str:
    builtin = engine.MODELS.get(engine.canon(model_key) or "")
    if builtin:
        return engine.model_option_label(builtin)
    row = custom_models.get_owned(con, user_id, model_key, active_only=False)
    return row["display_name"] if row else model_key


class CustomModelInput(BaseModel):
    name: str
    model_id: str
    base_url: str
    api_key: SecretStr | None = None


class ServiceConfigInput(BaseModel):
    image_enabled: bool = False
    image_provider: str = service_config.DEFAULT_IMAGE_PROVIDER
    image_base_url: str = ""
    image_model: str = ""
    image_api_key: SecretStr | None = None
    clear_image_api_key: bool = False
    search_enabled: bool = False
    search_base_url: str = ""
    search_api_key: SecretStr | None = None
    clear_search_api_key: bool = False
    max_tokens: int = service_config.DEFAULT_MAX_TOKENS
    streaming_enabled: bool = service_config.DEFAULT_STREAMING_ENABLED
    static_max_turns: int = service_config.DEFAULT_STATIC_MAX_TURNS
    static_subagent_max_turns: int = service_config.DEFAULT_STATIC_SUBAGENT_MAX_TURNS
    dynamic_max_turns: int = service_config.DEFAULT_DYNAMIC_MAX_TURNS


class DeckContinueRequest(BaseModel):
    message: str


class HistoryItemUpdate(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@app.get("/api/models/custom")
def list_custom_models(user=Depends(require_user), con=Depends(get_db)):
    return {
        "models": [custom_models.public_payload(row)
                   for row in custom_models.list_rows(con, user["id"])]
    }


@app.post("/api/models/custom", status_code=201)
def add_custom_model(body: CustomModelInput, user=Depends(require_user), con=Depends(get_db)):
    try:
        row = custom_models.create(
            con, user["id"], body.name, body.model_id, body.base_url,
            body.api_key.get_secret_value() if body.api_key else "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return custom_models.public_payload(row)


@app.delete("/api/models/custom/{model_id}")
def delete_custom_model(model_id: int, user=Depends(require_user), con=Depends(get_db)):
    key = custom_models.key_for(model_id)
    if not custom_models.delete(con, user["id"], key):
        raise HTTPException(status_code=404, detail="模型不存在")
    return {"ok": True, "key": key}


@app.get("/api/settings/services")
def get_service_config(user=Depends(require_user)):
    return service_config.public_payload(user["id"])


@app.put("/api/settings/services")
def save_service_config(body: ServiceConfigInput, user=Depends(require_user)):
    try:
        service_config.update(
            user["id"],
            image_enabled=body.image_enabled,
            image_provider=body.image_provider,
            image_base_url=body.image_base_url,
            image_model=body.image_model,
            image_api_key=(body.image_api_key.get_secret_value()
                           if body.image_api_key is not None else None),
            clear_image_api_key=body.clear_image_api_key,
            search_enabled=body.search_enabled,
            search_base_url=body.search_base_url,
            search_api_key=(body.search_api_key.get_secret_value()
                            if body.search_api_key is not None else None),
            clear_search_api_key=body.clear_search_api_key,
            max_tokens=body.max_tokens,
            streaming_enabled=body.streaming_enabled,
            static_max_turns=body.static_max_turns,
            static_subagent_max_turns=body.static_subagent_max_turns,
            dynamic_max_turns=body.dynamic_max_turns,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service_config.public_payload(user["id"])


# ---- deck generation API (Phase 2) ----------------------------------------
BATCH_MAX_QUERIES = int(os.environ.get("STUDIO_BATCH_MAX_QUERIES", "50"))
BATCH_UPLOAD_MAX_BYTES = int(os.environ.get("STUDIO_BATCH_UPLOAD_MAX_BYTES", str(2 * 1024 * 1024)))
BATCH_PACKAGE_MAX_BYTES = int(
    os.environ.get("STUDIO_BATCH_PACKAGE_MAX_BYTES", str(200 * 1024 * 1024))
)
BATCHES_DIR = DATA_DIR / "batches"


def _generation_selection(model: str, skill: str, con=None, user_id: int | None = None):
    model = str(model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="请先配置并选择生成模型")
    model_config = None
    if custom_models.id_from_key(model) is not None:
        row = custom_models.get_owned(con, user_id, model) if con is not None and user_id else None
        if not row:
            raise HTTPException(status_code=400, detail="自定义模型不存在或无权使用")
        model_config = custom_models.runtime_config(row)
    else:
        model = engine.canon(model)
        if model is None:
            raise HTTPException(status_code=400, detail="未知模型")
        selectable = {key for key, _ in engine.user_selectable_models()}
        if model not in selectable:
            raise HTTPException(status_code=400, detail="该模型未在当前部署中启用")
    skill = engine.canon_skill(skill)
    if skill is None:
        raise HTTPException(status_code=400, detail="未知 skill")
    pipeline = engine.pipeline_for_skill(skill)
    err = engine.validate_selection(model, pipeline, skill, model_config=model_config)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return model, pipeline, skill


_LANG_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "cn": "zh",
    "chinese": "zh",
    "中文": "zh",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "english": "en",
    "英文": "en",
}


def _normalize_language_hint(value, location="query"):
    """Normalize an optional user-supplied language field to zh/en."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{location} 的 lang 必须是字符串 zh 或 en")
    normalized = _LANG_ALIASES.get(value.strip().lower().replace("_", "-"), "")
    if not normalized:
        raise ValueError(f"{location} 的 lang 只支持 zh 或 en")
    return normalized


def _query_language(query: str) -> str:
    """Choose the instruction language from the query's dominant script."""
    text = str(query or "")
    han_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    latin_mass = latin_count / 5.0
    if han_count >= 2 and han_count >= latin_mass * 1.2:
        return "zh"
    if latin_count >= 3 and latin_mass > han_count * 1.2:
        return "en"
    if han_count:
        return "zh"
    if latin_count:
        return "en"
    return "zh"


def _resolve_auto_skill(skill: str, query: str, lang_hint: str = "") -> str:
    """Resolve Auto before enqueueing so the Harness always receives zh or en."""
    if skill != "auto":
        return skill
    return lang_hint if lang_hint in {"zh", "en"} else _query_language(query)


def _seed_for_query(
    query, skill, slide_count=0, theme="", style="", scheme="", dry=False,
    thinking=True, ppt_output="static_html",
):
    seed = {"query": query, "user_query": query, "thinking": bool(thinking)}
    language = engine.SKILLS[skill].get("deck_language", engine.SKILLS[skill]["language"])
    if language != "auto":
        seed["lang"] = language
    if slide_count and int(slide_count) > 0:
        seed["slide_count"] = int(slide_count)
    for key, value in (("theme", theme), ("style", style), ("scheme", scheme)):
        if value and value.strip():
            seed[key] = value.strip()
    seed["_dry"] = bool(dry)
    if skill == "sense-present-standard":
        seed["ppt_output"] = "static_html"
    elif skill == "sense-present-dazzle":
        seed["ppt_output"] = "dynamic_html"
    return seed


def _insert_deck(
    con, user_id, query, model, pipeline, skill, *,
    slide_count=0, theme="", style="", scheme="", dry=False, thinking=True,
    ppt_output="static_html",
    conversation_id=None, batch_id=None, batch_index=None, now=None,
):
    if not features.DYNAMIC_ENABLED and (
        ppt_output == "dynamic_html" or skill == "sense-present-dazzle"
    ):
        raise HTTPException(status_code=404, detail="V1 暂不开放动态演示")
    now = now or int(time.time())
    task_title = titles.fallback_title(query)
    cid = conversation_id or None
    if cid:
        owns = con.execute(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?", (cid, user_id)
        ).fetchone()
        if not owns:
            raise HTTPException(status_code=404, detail="对话不存在")
    else:
        cur = con.execute(
            "INSERT INTO conversations(user_id,title,created_at,updated_at) VALUES(?,?,?,?)",
            (user_id, task_title, now, now),
        )
        cid = cur.lastrowid

    con.execute(
        "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
        (cid, "user", query, now),
    )
    thinking_state = engine.resolve_thinking(model, bool(thinking))
    seed_store = _seed_for_query(
        query, skill, slide_count=slide_count, theme=theme, style=style,
        scheme=scheme, dry=dry, thinking=thinking_state["effective"],
        ppt_output=ppt_output,
    )
    seed_store["requested_thinking"] = thinking_state["requested"]
    seed_store["effective_thinking"] = thinking_state["effective"]
    seed_store["thinking_mode"] = thinking_state["mode"]
    seed_store["thinking_transport"] = thinking_state["transport"]
    seed_store["runtime_limits"] = service_config.generation_limits(user_id)
    cur = con.execute(
        "INSERT INTO decks("
        "user_id,conversation_id,batch_id,batch_index,title,seed_json,status,"
        "model,pipeline,skill_version,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, cid, batch_id, batch_index, task_title,
            json.dumps(seed_store, ensure_ascii=False), "queued",
            model, pipeline, skill, now,
        ),
    )
    deck_id = cur.lastrowid
    run_dir = engine.deck_run_dir(user_id, deck_id)
    con.execute("UPDATE decks SET run_dir = ? WHERE id = ?", (str(run_dir), deck_id))
    con.execute(
        "INSERT INTO messages(conversation_id,role,content,deck_id,created_at) VALUES(?,?,?,?,?)",
        (cid, "assistant", "正在生成 PPT…", deck_id, now),
    )
    con.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, cid))
    return deck_id, cid, seed_store


def _store_summarized_deck_title(deck_id: int, conversation_id: int, title: str):
    con = connect()
    try:
        con.execute("UPDATE decks SET title = ? WHERE id = ?", (title, deck_id))
        con.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
        con.commit()
    finally:
        con.close()


def _batch_query_entry(item, location):
    if isinstance(item, dict) and item.get("attachments"):
        raise ValueError(
            f"{location} 包含 attachments；请将清单与附件打成 ZIP 上传"
        )
    query = item.get("query") if isinstance(item, dict) else item
    if not isinstance(query, str):
        raise ValueError(f"{location} 缺少字符串 query")
    lang = _normalize_language_hint(
        item.get("lang") if isinstance(item, dict) else "",
        location,
    )
    return {"query": query, "lang": lang}


def _query_entries_from_json(value):
    if isinstance(value, dict):
        value = value.get("queries")
    if not isinstance(value, list):
        raise ValueError("JSON 顶层需为数组，或包含 queries 数组")
    return [
        _batch_query_entry(item, f"第 {index} 条")
        for index, item in enumerate(value, 1)
    ]


def _queries_from_json(value):
    """Backward-compatible query-only view used by older callers/tests."""
    return [entry["query"] for entry in _query_entries_from_json(value)]


def _parse_batch_query_entries(upload: UploadFile):
    raw = upload.file.read(BATCH_UPLOAD_MAX_BYTES + 1)
    if len(raw) > BATCH_UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="query 文件超过 2MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="query 文件必须是 UTF-8 编码")

    suffix = Path(upload.filename or "").suffix.lower()
    try:
        if suffix == ".json":
            entries = _query_entries_from_json(json.loads(text))
        elif suffix in (".jsonl", ".ndjson"):
            entries = []
            for line_no, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                entries.append(_batch_query_entry(item, f"第 {line_no} 行"))
        elif suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            fields = {str(name).strip().lower(): name for name in (reader.fieldnames or []) if name}
            column = next((fields[k] for k in ("query", "prompt", "brief") if k in fields), None)
            if not column:
                raise ValueError("CSV 需要 query 列")
            rows = list(reader)
            attachment_column = fields.get("attachments")
            if attachment_column and any(
                str(row.get(attachment_column, "") or "").strip() for row in rows
            ):
                raise ValueError("CSV 包含 attachments；请改用 ZIP + queries.jsonl")
            lang_column = fields.get("lang")
            entries = [
                {
                    "query": row.get(column, ""),
                    "lang": _normalize_language_hint(
                        row.get(lang_column, "") if lang_column else "",
                        f"CSV 第 {index + 1} 行",
                    ),
                }
                for index, row in enumerate(rows, 1)
            ]
        elif suffix in (".txt", ""):
            entries = [{"query": query, "lang": ""} for query in text.splitlines()]
        else:
            raise ValueError("仅支持 .txt、.csv、.json、.jsonl")
    except (json.JSONDecodeError, csv.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"query 文件格式错误：{exc}")

    entries = [
        {**entry, "query": entry["query"].strip()}
        for entry in entries
        if isinstance(entry.get("query"), str) and entry["query"].strip()
    ]
    if not entries:
        raise HTTPException(status_code=400, detail="query 文件中没有有效内容")
    if len(entries) > BATCH_MAX_QUERIES:
        raise HTTPException(
            status_code=400,
            detail=f"单批最多 {BATCH_MAX_QUERIES} 条 query，当前为 {len(entries)} 条",
        )
    too_long = next(
        (i for i, entry in enumerate(entries, 1) if len(entry["query"]) > 10000),
        None,
    )
    if too_long:
        raise HTTPException(status_code=400, detail=f"第 {too_long} 条 query 超过 10000 字符")
    return entries


def _parse_batch_queries(upload: UploadFile):
    """Backward-compatible query-only parser."""
    return [entry["query"] for entry in _parse_batch_query_entries(upload)]


def _safe_batch_zip_name(name: str) -> str:
    """Normalize a ZIP member/reference without ever extracting it to disk."""
    raw = str(name or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise ValueError(f"非法 ZIP 路径：{name!r}")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"非法 ZIP 路径：{name!r}")
    return "/".join(parts)


def _parse_batch_zip(upload: UploadFile):
    fileobj = upload.file
    fileobj.seek(0, os.SEEK_END)
    package_size = fileobj.tell()
    fileobj.seek(0)
    if package_size > BATCH_PACKAGE_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"批次 ZIP 超过 {BATCH_PACKAGE_MAX_BYTES // 1024 // 1024}MB",
        )

    try:
        with zipfile.ZipFile(fileobj) as archive:
            members = {}
            uncompressed_size = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _safe_batch_zip_name(info.filename)
                if name in members:
                    raise ValueError(f"ZIP 中存在重复文件：{name}")
                members[name] = info
                uncompressed_size += info.file_size
                if uncompressed_size > BATCH_PACKAGE_MAX_BYTES:
                    raise ValueError(
                        f"ZIP 解压后文件总量超过 "
                        f"{BATCH_PACKAGE_MAX_BYTES // 1024 // 1024}MB"
                    )

            manifests = [
                name for name in members
                if PurePosixPath(name).name.lower() == "queries.jsonl"
            ]
            if not manifests:
                raise ValueError("ZIP 中缺少 queries.jsonl")
            if len(manifests) > 1:
                raise ValueError("ZIP 中只能包含一个 queries.jsonl")

            manifest_name = manifests[0]
            manifest_info = members[manifest_name]
            if manifest_info.file_size > BATCH_UPLOAD_MAX_BYTES:
                raise ValueError("queries.jsonl 超过 2MB")
            try:
                manifest_text = archive.read(manifest_info).decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("queries.jsonl 必须是 UTF-8 编码") from exc

            manifest_dir = PurePosixPath(manifest_name).parent
            manifest_prefix = "" if str(manifest_dir) == "." else f"{manifest_dir}/"
            rows = []
            for line_no, line in enumerate(manifest_text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"queries.jsonl 第 {line_no} 行不是有效 JSON") from exc
                if not isinstance(item, dict):
                    raise ValueError(
                        f"queries.jsonl 第 {line_no} 行必须是包含 query 的对象"
                    )
                query = item.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError(f"queries.jsonl 第 {line_no} 行缺少字符串 query")
                lang = _normalize_language_hint(
                    item.get("lang"),
                    f"queries.jsonl 第 {line_no} 行",
                )

                refs = item.get("attachments", [])
                if refs is None:
                    refs = []
                elif isinstance(refs, str):
                    refs = [refs]
                if not isinstance(refs, list) or any(
                    not isinstance(ref, str) or not ref.strip() for ref in refs
                ):
                    raise ValueError(
                        f"queries.jsonl 第 {line_no} 行 attachments 必须是字符串数组"
                    )
                if len(refs) > attachments.MAX_FILES:
                    raise ValueError(
                        f"queries.jsonl 第 {line_no} 行附件超过 {attachments.MAX_FILES} 个"
                    )

                attachment_names = []
                seen = set()
                for ref in refs:
                    relative_name = _safe_batch_zip_name(ref)
                    member_name = manifest_prefix + relative_name
                    if member_name in seen:
                        continue
                    seen.add(member_name)
                    info = members.get(member_name)
                    if not info:
                        raise ValueError(
                            f"queries.jsonl 第 {line_no} 行引用的附件不存在：{ref}"
                        )
                    if info.file_size > attachments.MAX_FILE_BYTES:
                        raise ValueError(
                            f"附件 {ref} 超过 "
                            f"{attachments.MAX_FILE_BYTES // 1024 // 1024}MB"
                        )
                    attachment_names.append((relative_name, member_name))
                rows.append({
                    "query": query.strip(),
                    "lang": lang,
                    "attachment_names": attachment_names,
                    "line_no": line_no,
                })

            if not rows:
                raise ValueError("queries.jsonl 中没有有效内容")
            if len(rows) > BATCH_MAX_QUERIES:
                raise ValueError(
                    f"单批最多 {BATCH_MAX_QUERIES} 条 query，当前为 {len(rows)} 条"
                )
            too_long = next(
                (row["line_no"] for row in rows if len(row["query"]) > 10000), None
            )
            if too_long:
                raise ValueError(f"queries.jsonl 第 {too_long} 行 query 超过 10000 字符")

            data_cache = {}
            entries = []
            for row in rows:
                files = []
                for relative_name, member_name in row["attachment_names"]:
                    if member_name not in data_cache:
                        data_cache[member_name] = archive.read(members[member_name])
                    files.append({
                        "name": relative_name,
                        "data": data_cache[member_name],
                    })
                entries.append({
                    "query": row["query"],
                    "lang": row["lang"],
                    "attachments": files,
                })
            return entries
    except HTTPException:
        raise
    except (zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"批次 ZIP 格式错误：{exc}")


def _parse_batch_entries(upload: UploadFile):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix == ".zip":
        return _parse_batch_zip(upload)
    return [
        {**entry, "attachments": []}
        for entry in _parse_batch_query_entries(upload)
    ]


def _apply_deck_attachments(
    con, deck_id: int, query: str, seed_store: dict,
    files: list[UploadFile], attachment_mode: str,
):
    """Store one deck's files and update its seed with parsed/raw attachment metadata."""
    if not files:
        return
    mode = attachments.normalize_mode(attachment_mode)
    records = attachments.store_attachments(
        deck_id, files, parse=(mode == attachments.MODE_WEB_PARSE)
    )
    if not records:
        return
    # Keep the model-visible user message byte-for-byte equal to the user's
    # request. Attachment metadata is runtime context carried in the seed and
    # staged workspace, never prose appended to ``query``.
    seed_store["attachment_mode"] = mode
    seed_store["attachments"] = records
    if mode == attachments.MODE_PIPELINE_AGENT:
        raw = attachments.raw_attachments(records)
        if raw:
            seed_store["_raw_attachments"] = raw
    images = attachments.attachment_images(records)
    if images:
        seed_store["_attachment_images"] = images
    con.execute(
        "UPDATE decks SET seed_json = ? WHERE id = ?",
        (json.dumps(seed_store, ensure_ascii=False), deck_id),
    )


@app.post("/api/decks")
def create_deck(
    request: Request,
    query: str = Form(...),
    lang: str = Form("zh"),
    slide_count: int = Form(0),
    theme: str = Form(""),
    style: str = Form(""),
    scheme: str = Form(""),
    thinking: int = Form(1),
    dry: int = Form(0),
    model: str = Form(""),
    pipeline: str = Form(""),
    skill: str = Form(""),
    ppt_output: str = Form("static_html"),
    attachment_mode: str = Form(""),
    font_roles: str = Form(""),
    font_license_ack: int = Form(0),
    conversation_id: int = Form(0),
    files: list[UploadFile] = File([]),
    font_files: list[UploadFile] = File([]),
    user=Depends(require_user),
    con=Depends(get_db),
):
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    # Skill 是版本真源;忽略客户端提交的旧 pipeline 值,强制选择同版本 Harness。
    model, pipeline, skill = _generation_selection(model, skill, con, user["id"])
    skill = _resolve_auto_skill(skill, query)
    pipeline = engine.pipeline_for_skill(skill)
    now = int(time.time())

    deck_id, cid, seed_store = _insert_deck(
        con, user["id"], query, model, pipeline, skill,
        slide_count=slide_count, theme=theme, style=style, scheme=scheme, dry=dry,
        thinking=bool(thinking), ppt_output=ppt_output,
        conversation_id=conversation_id, now=now,
    )

    # 附件默认 web_parse:Studio 先解析为文本/工作区图片引用,再拼到 query。
    # pipeline_agent 模式只保存原始附件,由管线侧先派 attachment_reader 子 agent 处理。
    if files and "attachments" in engine.PIPELINES[pipeline].get("caps", []):
        _apply_deck_attachments(
            con, deck_id, query, seed_store, files, attachment_mode
        )

    font_config = custom_fonts.store_custom_fonts(
        deck_id, font_files, font_roles, bool(font_license_ack)
    )
    if font_config:
        seed_store["font_config"] = font_config
        con.execute(
            "UPDATE decks SET seed_json = ? WHERE id = ?",
            (json.dumps(seed_store, ensure_ascii=False), deck_id),
        )

    con.commit()

    jobs.enqueue(deck_id)
    title_model = dynamic._model_registry(user).get(model) or {}
    titles.schedule_summary(
        query, title_model,
        lambda title: _store_summarized_deck_title(deck_id, cid, title),
    )
    return {"deck_id": deck_id, "conversation_id": cid, "status": "queued"}


@app.post("/api/decks/{deck_id}/continue")
def continue_deck(
    deck_id: int,
    body: DeckContinueRequest,
    user=Depends(require_user),
    con=Depends(get_db),
):
    """Continue editing a completed static Deck in its existing workspace.

    The Deck id, history entry and output directory remain stable.  Conversation
    turns are appended to the same conversation while the revision worker edits
    the existing formal artifacts in place.
    """
    # Serialize status check + update so a double click cannot schedule two
    # revision workers against the same workspace.
    con.execute("BEGIN IMMEDIATE")
    deck = _own_deck(con, deck_id, user["id"])
    if not deck:
        raise HTTPException(status_code=404, detail="deck 不存在")
    pipeline = engine.PIPELINES.get(deck["pipeline"] or "")
    if not pipeline or "revision" not in pipeline.get("caps", []):
        raise HTTPException(status_code=409, detail="该静态生成版本暂不支持续编")
    if deck["status"] != "completed" or jobs._engine_alive(deck_id):
        raise HTTPException(status_code=409, detail="请等当前生成完成后再提交修改要求")
    instruction = body.message.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="修改要求不能为空")
    if len(instruction) > 8000:
        raise HTTPException(status_code=413, detail="修改要求超过 8000 字符")
    now = int(time.time())
    if deck["conversation_id"]:
        revision_no = int(con.execute(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM decks "
            "WHERE user_id = ? AND conversation_id = ?",
            (user["id"], deck["conversation_id"]),
        ).fetchone()[0])
        revision_no = max(revision_no, int(deck["revision_no"] or 0) + 1)
    else:
        revision_no = max(1, int(deck["revision_no"] or 0) + 1)
    seed = json.loads(deck["seed_json"] or "{}")
    seed.pop("_dry", None)
    seed["_revision"] = {
        "parent_deck_id": deck_id,
        "revision_no": revision_no,
        "instruction": instruction,
        "in_place": True,
    }
    con.execute(
        "UPDATE decks SET seed_json=?,status='queued',revision_no=?,"
        "revision_instruction=?,error=NULL,started_at=NULL,finished_at=NULL "
        "WHERE id=?",
        (json.dumps(seed, ensure_ascii=False), revision_no, instruction, deck_id),
    )
    if deck["conversation_id"]:
        con.execute(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
            (deck["conversation_id"], "user", instruction, now),
        )
        con.execute(
            "INSERT INTO messages(conversation_id,role,content,deck_id,created_at) "
            "VALUES(?,?,?,?,?)",
            (deck["conversation_id"], "assistant", "正在原成稿上继续修改…", deck_id, now),
        )
        con.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, deck["conversation_id"]),
        )
    con.commit()
    jobs.enqueue(deck_id)
    return {
        "deck_id": deck_id,
        "parent_deck_id": deck["parent_deck_id"],
        "revision_no": revision_no,
        "status": "queued",
        "in_place": True,
    }


def _batch_payload(con, row, include_decks=False):
    decks = con.execute(
        "SELECT id,batch_index,title,status,skill_version,slide_count,error,"
        "created_at,started_at,finished_at "
        "FROM decks WHERE batch_id = ? AND user_id = ? ORDER BY batch_index,id",
        (row["id"], row["user_id"]),
    ).fetchall()
    counts = {"waiting": 0, "queued": 0, "running": 0, "completed": 0, "failed": 0}
    for deck in decks:
        status = deck["status"]
        if status in ("failed", "rejected"):
            status = "failed"
        counts[status] = counts.get(status, 0) + 1
    total = len(decks)
    active = counts["waiting"] + counts["queued"] + counts["running"]
    if active:
        status = "running" if counts["running"] else "queued"
    elif counts["completed"] == total and total:
        status = "completed"
    elif counts["completed"]:
        status = "partial"
    else:
        status = "failed"
    payload = {
        "id": row["id"],
        "name": row["name"],
        "source_name": row["source_name"],
        "model": row["model"],
        "model_label": _model_label(con, row["user_id"], row["model"]),
        "pipeline": row["pipeline"],
        "skill_version": row["skill_version"],
        "total": total,
        "counts": counts,
        "status": status,
        "terminal": active == 0,
        "download_ready": active == 0 and counts["completed"] > 0,
        "created_at": row["created_at"],
    }
    if include_decks:
        payload["decks"] = [dict(deck) for deck in decks]
    return payload


def _own_batch(con, batch_id, uid):
    return con.execute(
        "SELECT * FROM batches WHERE id = ? AND user_id = ?", (batch_id, uid)
    ).fetchone()


@app.post("/api/batches")
def create_batch(
    query_file: UploadFile = File(...),
    name: str = Form(""),
    model: str = Form(""),
    skill: str = Form(""),
    slide_count: int = Form(0),
    theme: str = Form(""),
    style: str = Form(""),
    scheme: str = Form(""),
    thinking: int = Form(1),
    attachment_mode: str = Form(attachments.MODE_WEB_PARSE),
    user=Depends(require_user),
    con=Depends(get_db),
):
    """Create one deck per query; ZIP packages may bind different files per query."""
    entries = _parse_batch_entries(query_file)
    model, pipeline, skill = _generation_selection(model, skill, con, user["id"])
    has_attachments = any(entry["attachments"] for entry in entries)
    if has_attachments and "attachments" not in engine.PIPELINES[pipeline].get("caps", []):
        raise HTTPException(status_code=400, detail="当前 Skill/Harness 不支持附件")
    attachment_mode = (
        attachments.normalize_mode(attachment_mode)
        if has_attachments else attachments.MODE_WEB_PARSE
    )
    now = int(time.time())
    source_name = Path(query_file.filename or "queries.txt").name[:180]
    batch_name = (name.strip() or Path(source_name).stem.strip() or f"batch-{now}")[:80]
    cur = con.execute(
        "INSERT INTO batches("
        "user_id,name,source_name,model,pipeline,skill_version,total_count,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (user["id"], batch_name, source_name, model, pipeline, skill, len(entries), now),
    )
    batch_id = cur.lastrowid
    deck_ids = []
    for index, entry in enumerate(entries, 1):
        query = entry["query"]
        deck_skill = _resolve_auto_skill(skill, query, entry.get("lang", ""))
        deck_pipeline = engine.pipeline_for_skill(deck_skill)
        deck_id, _, seed_store = _insert_deck(
            con, user["id"], query, model, deck_pipeline, deck_skill,
            slide_count=slide_count, theme=theme, style=style, scheme=scheme,
            thinking=bool(thinking),
            batch_id=batch_id, batch_index=index, now=now,
        )
        package_files = [
            UploadFile(filename=item["name"], file=io.BytesIO(item["data"]))
            for item in entry["attachments"]
        ]
        if package_files:
            _apply_deck_attachments(
                con, deck_id, query, seed_store, package_files, attachment_mode
            )
        deck_ids.append(deck_id)
    con.commit()
    for deck_id in deck_ids:
        jobs.enqueue(deck_id)
    row = _own_batch(con, batch_id, user["id"])
    return _batch_payload(con, row, include_decks=True)


@app.get("/api/batches")
def list_batches(user=Depends(require_user), con=Depends(get_db)):
    rows = con.execute(
        "SELECT * FROM batches WHERE user_id = ? ORDER BY id DESC LIMIT 50", (user["id"],)
    ).fetchall()
    return {"batches": [_batch_payload(con, row) for row in rows]}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_batch(con, batch_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="批次不存在")
    return _batch_payload(con, row, include_decks=True)


@app.get("/api/decks")
def list_decks(user=Depends(require_user), con=Depends(get_db)):
    rows = con.execute(
        "SELECT id,batch_id,batch_index,parent_deck_id,revision_no,title,status,"
        "slide_count,created_at,started_at,finished_at,seed_json "
        "FROM decks WHERE user_id = ? ORDER BY id DESC", (user["id"],)
    ).fetchall()
    prefs = {
        row["item_id"]: bool(row["pinned"])
        for row in con.execute(
            "SELECT item_id,pinned FROM history_preferences "
            "WHERE user_id = ? AND item_kind = 'static'",
            (user["id"],),
        ).fetchall()
    }
    decks = []
    for row in rows:
        item = dict(row)
        try:
            seed = json.loads(item.pop("seed_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            seed = {}
        if not features.DYNAMIC_ENABLED and seed.get("ppt_output") == "dynamic_html":
            continue
        item["display_title"] = titles.display_title(item.get("title"), seed.get("user_query") or seed.get("query") or "")
        item["presentation_kind"] = (
            "dynamic" if seed.get("ppt_output") == "dynamic_html" else "static"
        )
        item["pinned"] = prefs.get(str(item["id"]), False)
        decks.append(item)
    return {"decks": decks}


@app.patch("/api/history/{kind}/{item_id}")
def update_history_item(
    kind: str,
    item_id: str,
    body: HistoryItemUpdate,
    user=Depends(require_user),
    con=Depends(get_db),
):
    """Persist sidebar-only metadata shared by every Studio frontend."""
    if kind not in {"static", "dynamic"}:
        raise HTTPException(status_code=400, detail="未知演示类型")
    if kind == "dynamic" and not features.DYNAMIC_ENABLED:
        raise HTTPException(status_code=404, detail="V1 暂未开放动态演示")
    title = None
    if body.title is not None:
        title = re.sub(r"\s+", " ", body.title).strip()
        if not title:
            raise HTTPException(status_code=400, detail="名称不能为空")
        if len(title) > 80:
            raise HTTPException(status_code=400, detail="名称最多 80 个字符")

    if kind == "static":
        try:
            deck_id = int(item_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="演示编号无效") from exc
        row = _own_deck(con, deck_id, user["id"])
        if not row:
            raise HTTPException(status_code=404, detail="演示不存在")
        if title is not None:
            if row["conversation_id"]:
                con.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (title, int(time.time()), row["conversation_id"], user["id"]),
                )
                con.execute(
                    "UPDATE decks SET title = ? WHERE conversation_id = ? AND user_id = ?",
                    (title, row["conversation_id"], user["id"]),
                )
            else:
                con.execute("UPDATE decks SET title = ? WHERE id = ?", (title, deck_id))
    else:
        if not dynamic._valid_conv(item_id) or not dynamic.runtime._conv_dir(item_id).is_dir():
            raise HTTPException(status_code=404, detail="演示不存在")
        if not dynamic._can_access(item_id, user):
            raise HTTPException(status_code=403, detail="无权修改该演示")
        if title is not None:
            meta = dynamic.runtime._read_meta(item_id)
            meta["title"] = title
            meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            dynamic.runtime._write_meta(item_id, meta)

    if body.pinned is not None:
        con.execute(
            "INSERT INTO history_preferences(user_id,item_kind,item_id,pinned,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,item_kind,item_id) DO UPDATE SET "
            "pinned=excluded.pinned,updated_at=excluded.updated_at",
            (user["id"], kind, item_id, int(body.pinned), int(time.time())),
        )
    con.commit()
    return {"ok": True, "kind": kind, "id": item_id, "title": title, "pinned": body.pinned}


@app.get("/api/decks/{deck_id}")
def get_deck(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    return dict(row)


@app.post("/api/decks/{deck_id}/retry")
async def retry_deck(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    """失败/中断的任务重新排队跑(引擎不支持断点续作,等价从头重新生成)。"""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    if row["status"] not in ("failed", "rejected", "interrupted"):
        raise HTTPException(status_code=409, detail="仅失败的任务可以重新生成")
    is_custom = custom_models.id_from_key(row["model"]) is not None
    custom_row = custom_models.get_owned(
        con, user["id"], row["model"], active_only=False
    ) if is_custom else None
    if (is_custom and not custom_row) or (not is_custom and engine.canon(row["model"]) is None):
        raise HTTPException(status_code=409, detail="该任务使用的模型已下线，请使用当前可选模型新建任务")
    if jobs._engine_alive(deck_id):
        raise HTTPException(status_code=409, detail="该任务的引擎仍在运行")
    con.execute(
        "UPDATE decks SET status='queued', error=NULL, started_at=NULL, finished_at=NULL "
        "WHERE id = ?", (deck_id,))
    con.commit()
    jobs.enqueue(deck_id)
    return {"ok": True, "status": "queued"}


def _regenerated_seed(source_seed: dict, source_deck_id: int, new_deck_id: int) -> dict:
    """Clone one completed request without retaining revision-only state.

    Uploaded attachments and custom fonts live outside the generated workspace.
    Copy that immutable input directory and rewrite its absolute paths so the
    regenerated task remains self-contained even if the old history item is
    deleted later.
    """
    source_uploads = engine.deck_uploads_dir(source_deck_id)
    new_uploads = engine.deck_uploads_dir(new_deck_id)
    if source_uploads.is_dir():
        shutil.copytree(source_uploads, new_uploads, dirs_exist_ok=True)
    serialized = json.dumps(source_seed, ensure_ascii=False)
    serialized = serialized.replace(str(source_uploads), str(new_uploads))
    seed = json.loads(serialized)
    seed.pop("_revision", None)
    seed["_dry"] = False
    return seed


@app.post("/api/decks/{deck_id}/regenerate")
def regenerate_deck(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    """Create a new task from a completed deck while preserving the old result."""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="仅已完成的演示可以再次生成")
    model, pipeline, skill = _generation_selection(
        row["model"], row["skill_version"], con, user["id"]
    )
    source_seed = json.loads(row["seed_json"] or "{}")
    query = str(source_seed.get("user_query") or source_seed.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=409, detail="原任务缺少生成需求，无法再次生成")
    now = int(time.time())
    new_id, conversation_id, _ = _insert_deck(
        con, user["id"], query, model, pipeline, skill,
        slide_count=int(source_seed.get("slide_count") or 0),
        theme=str(source_seed.get("theme") or ""),
        style=str(source_seed.get("style") or ""),
        scheme=str(source_seed.get("scheme") or ""),
        thinking=bool(source_seed.get("requested_thinking", source_seed.get("thinking", True))),
        ppt_output=str(source_seed.get("ppt_output") or "static_html"),
        now=now,
    )
    try:
        cloned_seed = _regenerated_seed(source_seed, deck_id, new_id)
    except Exception:
        shutil.rmtree(engine.deck_uploads_dir(new_id), ignore_errors=True)
        raise
    con.execute(
        "UPDATE decks SET title=?,seed_json=? WHERE id=?",
        (row["title"], json.dumps(cloned_seed, ensure_ascii=False), new_id),
    )
    con.execute(
        "UPDATE conversations SET title=? WHERE id=?",
        (row["title"], conversation_id),
    )
    con.commit()
    jobs.enqueue(new_id)
    return {"ok": True, "deck_id": new_id, "status": "queued", "source_deck_id": deck_id}


@app.post("/api/decks/{deck_id}/cancel")
async def cancel_deck(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    killed = await jobs.cancel(deck_id)
    return {"ok": True, "killed": killed}


def _own_deck(con, deck_id, uid):
    row = con.execute(
        "SELECT * FROM decks WHERE id = ? AND user_id = ?", (deck_id, uid)
    ).fetchone()
    if row and not features.DYNAMIC_ENABLED:
        try:
            seed = json.loads(row["seed_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            seed = {}
        if seed.get("ppt_output") == "dynamic_html":
            return None
    return row


@app.delete("/api/decks/{deck_id}")
async def delete_deck(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    active_revision = con.execute(
        "SELECT id FROM decks WHERE parent_deck_id = ? AND user_id = ? "
        "AND status IN ('waiting','queued','running') ORDER BY id LIMIT 1",
        (deck_id, user["id"]),
    ).fetchone()
    if active_revision:
        raise HTTPException(
            status_code=409,
            detail=f"修订任务 #{active_revision['id']} 尚未结束，暂不能删除父版本",
        )
    # 正在生成的先停掉引擎,再删档
    if row["status"] in ("waiting", "queued", "running"):
        await jobs.cancel(deck_id)
        await asyncio.sleep(1)          # 给引擎进程一点退出时间,避免删目录时还在写
    import shutil
    if row["run_dir"]:
        shutil.rmtree(row["run_dir"], ignore_errors=True)
    for p in (engine.JOBS_DIR / f"{deck_id}.json", engine.log_path(deck_id)):
        try:
            os.remove(p)
        except OSError:
            pass
    if row["conversation_id"]:
        siblings = con.execute(
            "SELECT COUNT(*) AS n FROM decks WHERE conversation_id = ? AND id != ?",
            (row["conversation_id"], deck_id),
        ).fetchone()["n"]
        if not siblings:
            con.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (row["conversation_id"], user["id"]),
            )  # messages 级联删除
    con.execute("DELETE FROM decks WHERE id = ?", (deck_id,))  # deck_usage 级联删除
    con.execute(
        "DELETE FROM history_preferences WHERE user_id = ? AND item_kind = 'static' AND item_id = ?",
        (user["id"], str(deck_id)),
    )
    con.commit()
    return {"ok": True}


def _spec_prefs(seed: dict) -> dict:
    """deck 的内容偏好(篇幅/主题/风格/色系)→ 前端「配方」溯源 chip;空字段前端自动不展示。"""
    return {
        k: seed.get(k)
        for k in (
            "slide_count", "theme", "style", "scheme", "thinking",
            "requested_thinking", "effective_thinking", "thinking_mode",
            "thinking_transport", "runtime_limits",
        )
    }


def _progress_seed_payload(seed: dict) -> dict:
    payload = _spec_prefs(seed)
    query = seed.get("query")
    payload["query"] = query if isinstance(query, str) else ""
    user_query = seed.get("user_query")
    if not isinstance(user_query, str):
        user_query = payload["query"]
        for marker in ("\n\n【附件资料】", "\n\n【附件处理模式:pipeline_agent】"):
            user_query = user_query.split(marker, 1)[0]
    payload["user_query"] = user_query
    payload["attachments"] = []
    attachment_mode = str(seed.get("attachment_mode") or "")
    for index, item in enumerate(seed.get("attachments") or []):
        if not isinstance(item, dict):
            continue
        stored_name = Path(str(item.get("stored_name") or "")).name
        preview_rel = ""
        # Parsed image inputs are staged directly into the workspace. Raw-mode
        # inputs preserve the original under attachments/raw/. Exposing only
        # this safe relative path lets the live UI preview attachments through
        # the existing deck-file endpoint even before a backend process reloads
        # the newer dedicated attachment route.
        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            candidate = str(image.get("workspace_rel") or "")
            if candidate.startswith("attachments/"):
                preview_rel = candidate
                break
        if not preview_rel and attachment_mode == attachments.MODE_PIPELINE_AGENT and stored_name:
            preview_rel = f"attachments/raw/{stored_name}"
        payload["attachments"].append({
            "index": index,
            "name": item.get("name") or stored_name or "附件",
            "stored_name": stored_name,
            "type": item.get("type") or "file",
            "size": int(item.get("size") or 0),
            "preview_rel": preview_rel,
        })
    return payload


def _deck_conversation_turns(con, row, seed: dict) -> list[dict]:
    """Return user/assistant turns through the selected Deck's latest response.

    Current continuations reuse one Deck id, so the final assistant message for
    that id is current and earlier messages for the same id are completed turns.
    Legacy immutable child revisions remain readable with the same response.
    """
    conversation_id = row["conversation_id"] if "conversation_id" in row.keys() else None
    current_id = int(row["id"])
    if not conversation_id:
        return [
            {
                "role": "user", "content": seed.get("user_query") or seed.get("query") or "",
                "created_at": row["created_at"],
            },
            {
                "role": "assistant", "content": "正在生成 PPT…", "deck_id": current_id,
                "status": row["status"], "slide_count": row["slide_count"],
                "revision_no": int(row["revision_no"] or 0), "current": True,
                "created_at": row["started_at"] or row["created_at"],
                "responded_at": row["finished_at"],
            },
        ]
    messages = con.execute(
        "SELECT m.id,m.role,m.content,m.deck_id,m.created_at,"
        "d.status AS deck_status,d.slide_count AS deck_slide_count,"
        "d.revision_no AS deck_revision_no,d.finished_at AS deck_finished_at "
        "FROM messages m LEFT JOIN decks d ON d.id = m.deck_id "
        "WHERE m.conversation_id = ? ORDER BY m.id",
        (conversation_id,),
    ).fetchall()
    current_assistant_ids = [
        int(message["id"]) for message in messages
        if message["role"] == "assistant" and message["deck_id"] is not None
        and int(message["deck_id"]) == current_id
    ]
    current_message_id = current_assistant_ids[-1] if current_assistant_ids else None
    current_revision = int(row["revision_no"] or 0)
    revision_by_message = {
        message_id: max(0, current_revision - (len(current_assistant_ids) - index - 1))
        for index, message_id in enumerate(current_assistant_ids)
    }
    turns = []
    for message in messages:
        role = message["role"]
        if role not in ("user", "assistant"):
            continue
        item = {
            "role": role,
            "content": message["content"] or "",
            "created_at": message["created_at"],
        }
        deck_id = message["deck_id"]
        if role == "assistant" and deck_id is not None:
            message_id = int(message["id"])
            is_current = int(deck_id) == current_id and message_id == current_message_id
            item.update({
                "deck_id": int(deck_id),
                "status": (
                    row["status"] if is_current else
                    ("completed" if int(deck_id) == current_id else (message["deck_status"] or "completed"))
                ),
                "slide_count": message["deck_slide_count"],
                "revision_no": (
                    revision_by_message.get(message_id, 0)
                    if int(deck_id) == current_id else int(message["deck_revision_no"] or 0)
                ),
                "current": is_current,
            })
            # Immutable legacy child decks have an unambiguous completion
            # timestamp. In-place revisions reuse one deck id, so only the
            # current assistant turn may safely use the deck's finished_at.
            if is_current:
                item["responded_at"] = row["finished_at"]
            elif int(deck_id) != current_id:
                item["responded_at"] = message["deck_finished_at"]
        turns.append(item)
        if item.get("current"):
            break
    # Legacy rows may predate messages. Keep the selected deck usable and make
    # the current assistant turn explicit for the frontend timeline.
    if not turns or turns[0].get("role") != "user":
        turns.insert(0, {
            "role": "user", "content": seed.get("user_query") or seed.get("query") or "",
            "created_at": row["created_at"],
        })
    if not any(turn.get("current") for turn in turns):
        turns.append({
            "role": "assistant", "content": "正在生成 PPT…", "deck_id": current_id,
            "status": row["status"], "slide_count": row["slide_count"],
            "revision_no": int(row["revision_no"] or 0), "current": True,
            "created_at": row["started_at"] or row["created_at"],
            "responded_at": row["finished_at"],
        })
    return turns


@app.get("/api/decks/{deck_id}/progress")
def deck_progress(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    seed = json.loads(row["seed_json"])
    seed.pop("_dry", None)
    prog = trace.snapshot(row["run_dir"], seed=seed, status=row["status"],
                          started_at=row["started_at"], finished_at=row["finished_at"],
                          log_path=str(engine.log_path(deck_id)))
    if row["status"] in ("failed", "rejected", "interrupted"):
        prog["error"] = row["error"]
    prog["deck_id"] = deck_id
    prog["title"] = row["title"]
    prog["model"] = row["model"] if "model" in row.keys() else None
    prog["pipeline"] = row["pipeline"] if "pipeline" in row.keys() else None
    prog["skill_version"] = row["skill_version"] if "skill_version" in row.keys() else None
    prog["parent_deck_id"] = row["parent_deck_id"] if "parent_deck_id" in row.keys() else None
    prog["revision_no"] = row["revision_no"] if "revision_no" in row.keys() else 0
    prog["revision_instruction"] = (
        row["revision_instruction"] if "revision_instruction" in row.keys() else None
    )
    prog["conversation_turns"] = _deck_conversation_turns(con, row, seed)
    pipeline = engine.PIPELINES.get(row["pipeline"] or "")
    prog["revision_supported"] = bool(
        pipeline and "revision" in pipeline.get("caps", [])
    )
    html_preview = _static_html_preview_state(row["run_dir"])
    prog["html_ready"] = html_preview["ready"]
    prog["html_final"] = html_preview["final"]
    prog["html_stamp"] = html_preview["stamp"]
    prog["html_entry"] = html_preview.get("entry", "present.html")
    prog["ppt_output"] = seed.get("ppt_output", "static_html")
    prog.update(_progress_seed_payload(seed))
    return prog


@app.get("/api/decks/{deck_id}/output-location")
def deck_output_location(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    """Return the owned job's server-side output directory for local workflows."""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    if not row["run_dir"]:
        raise HTTPException(status_code=409, detail="输出目录尚未创建")
    return {"path": str(Path(row["run_dir"]).resolve())}


@app.get("/api/decks/{deck_id}/events")
async def deck_events(deck_id: int, request: Request, user=Depends(require_user)):
    con = connect()
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="deck 不存在")
    seed = json.loads(row["seed_json"])
    seed.pop("_dry", None)
    conversation_turns = _deck_conversation_turns(con, row, seed)
    con.close()
    run_dir = row["run_dir"]
    # 静态元信息(生成期不变):SSE 每帧都带上 model/pipeline/skill_version,与 /progress 一致。
    # 否则前端 renderStatus 会用空标签重绘 #ed-status → 右上角标签一闪即消。
    meta = {
        "model": row["model"] if "model" in row.keys() else None,
        "pipeline": row["pipeline"] if "pipeline" in row.keys() else None,
        "skill_version": row["skill_version"] if "skill_version" in row.keys() else None,
        "parent_deck_id": row["parent_deck_id"] if "parent_deck_id" in row.keys() else None,
        "revision_no": row["revision_no"] if "revision_no" in row.keys() else 0,
        "revision_instruction": (
            row["revision_instruction"]
            if "revision_instruction" in row.keys() else None
        ),
        "conversation_turns": conversation_turns,
        "revision_supported": bool(
            engine.PIPELINES.get(row["pipeline"] or "")
            and "revision" in engine.PIPELINES[row["pipeline"]].get("caps", [])
        ),
        "ppt_output": seed.get("ppt_output", "static_html"),
        "html_entry": _static_html_preview_state(row["run_dir"]).get("entry", "present.html"),
        **_progress_seed_payload(seed),
    }

    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            con = connect()
            r = con.execute("SELECT status, started_at, finished_at, error FROM decks WHERE id = ?",
                            (deck_id,)).fetchone()
            con.close()
            status = r["status"] if r else "failed"
            prog = trace.snapshot(run_dir, seed=seed, status=status,
                                  started_at=r["started_at"] if r else None,
                                  finished_at=r["finished_at"] if r else None,
                                  log_path=str(engine.log_path(deck_id)))
            if status in ("failed", "rejected", "interrupted"):
                prog["error"] = r["error"] if r else "missing"
            html_preview = _static_html_preview_state(run_dir)
            prog["html_ready"] = html_preview["ready"]
            prog["html_final"] = html_preview["final"]
            prog["html_stamp"] = html_preview["stamp"]
            prog.update(meta)                       # 右上角标签(model/pipeline/skill)每帧都在
            payload = json.dumps(prog, ensure_ascii=False)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if status in ("completed", "failed", "rejected", "interrupted"):
                break
            await asyncio.sleep(1.5)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _safe_target(row, rel):
    base = os.path.realpath(row["run_dir"])
    target = os.path.realpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(status_code=403, detail="非法路径")
    return target


_SLIDE_FRAGMENT_RE = re.compile(r"^slide_(\d+)\.html$")


def _static_html_preview_state(run_dir: str | None) -> dict:
    """Report whether a formal or fragment-backed live HTML preview is available."""
    if not run_dir:
        return {"ready": False, "final": False, "stamp": 0}
    root = Path(run_dir)
    present = root / "present.html"
    # SenseNova PPT v2 dynamic output uses the same Studio deck shell but its
    # canonical player is deck.html.  It implements ?slide=N and slidechange.
    dynamic_deck = root / "deck.html"
    if dynamic_deck.is_file() and not present.is_file():
        try:
            stamp = dynamic_deck.stat().st_mtime_ns // 1_000_000
        except OSError:
            stamp = 0
        return {"ready": True, "final": True, "stamp": stamp, "entry": "deck.html"}
    slides_dir = root / "slides"
    try:
        fragments = sorted(
            path for path in slides_dir.glob("slide_*.html")
            if path.is_file() and _SLIDE_FRAGMENT_RE.match(path.name) and path.stat().st_size > 0
        )
    except OSError:
        fragments = []
    if not fragments or not (root / "base.css").is_file():
        if not present.is_file():
            return {"ready": False, "final": False, "stamp": 0}
        try:
            stamp = present.stat().st_mtime_ns // 1_000_000
        except OSError:
            stamp = 0
        return {"ready": True, "final": True, "stamp": stamp}
    # Refresh the live player after a successful render when possible. During
    # the first page, fall back to fragment mtimes so preview becomes available
    # before the whole deck is finalized.
    rendered = [path for path in (root / "renders").glob("slide_*.png") if path.is_file()]
    stamp_sources = fragments + rendered
    try:
        live_stamp = max(path.stat().st_mtime_ns // 1_000_000 for path in stamp_sources)
    except (OSError, ValueError):
        live_stamp = 0
    if present.is_file():
        try:
            present_stamp = present.stat().st_mtime_ns // 1_000_000
        except OSError:
            present_stamp = 0
        # In-place revisions intentionally keep the last good present.html on
        # disk while individual slide fragments are rewritten.  A newer
        # fragment/render therefore means the canonical player is stale: serve
        # the fragment-backed player until the final build catches up.
        if live_stamp <= present_stamp:
            return {"ready": True, "final": True, "stamp": present_stamp}
    return {"ready": True, "final": False, "stamp": live_stamp}


def _provisional_present_html(run_dir: str) -> str:
    """Build a live player around authored slide documents without rewriting them."""
    root = Path(run_dir)
    css_path = root / "base.css"
    if not css_path.is_file():
        raise HTTPException(status_code=404, detail="实时 PPT 样式尚未生成")
    rows: list[tuple[int, str]] = []
    for path in (root / "slides").glob("slide_*.html"):
        match = _SLIDE_FRAGMENT_RE.match(path.name)
        if not match or not path.is_file():
            continue
        try:
            if path.stat().st_size > 0:
                rows.append((int(match.group(1)), path.name))
        except OSError:
            continue
    rows.sort(key=lambda row: row[0])
    if not rows:
        raise HTTPException(status_code=404, detail="实时 PPT 页面尚未生成")
    css = css_path.read_text(encoding="utf-8")
    width_match = re.search(r"--canvas-w:\s*(\d+)px", css)
    height_match = re.search(r"--canvas-h:\s*(\d+)px", css)
    canvas_w = int(width_match.group(1)) if width_match else 1280
    canvas_h = int(height_match.group(1)) if height_match else 720
    frames = "\n".join(
        f'    <iframe class="provisional-slide" data-slide="{number}" '
        f'src="slides/{filename}" title="Slide {number}" scrolling="no"></iframe>'
        for number, filename in rows
    )
    player = f"""
  <script>
  (() => {{
    const slides = [...document.querySelectorAll('iframe.provisional-slide[data-slide]')];
    const byNumber = new Map(slides.map(slide => [Number(slide.dataset.slide), slide]));
    const ordered = [...byNumber.keys()].filter(Number.isFinite).sort((a, b) => a - b);
    let current = Number(new URLSearchParams(location.search).get('slide')) || ordered[0] || 1;
    function fitDeck() {{
      const scale = Math.min(innerWidth / {canvas_w}, innerHeight / {canvas_h});
      const deck = document.getElementById('deck');
      deck.style.left = `${{(innerWidth - {canvas_w} * scale) / 2}}px`;
      deck.style.top = `${{(innerHeight - {canvas_h} * scale) / 2}}px`;
      deck.style.transform = `scale(${{scale}})`;
    }}
    function go(number) {{
      if (!byNumber.has(Number(number))) return;
      current = Number(number);
      slides.forEach(slide => {{
        const active = Number(slide.dataset.slide) === current;
        slide.classList.toggle('active', active);
        slide.setAttribute('aria-hidden', active ? 'false' : 'true');
      }});
      dispatchEvent(new CustomEvent('slidechange', {{ detail: {{ slide: current }} }}));
    }}
    function step(delta) {{
      const index = ordered.indexOf(current);
      go(ordered[Math.max(0, Math.min(ordered.length - 1, index + delta))]);
    }}
    addEventListener('keydown', event => {{
      if (['ArrowRight', 'PageDown', ' '].includes(event.key)) step(1);
      if (['ArrowLeft', 'PageUp'].includes(event.key)) step(-1);
    }});
    addEventListener('resize', fitDeck);
    fitDeck();
    function waitForFrame(frame) {{
      return new Promise(resolve => {{
        let settled = false;
        const finish = () => {{
          if (settled) return;
          settled = true;
          try {{
            Promise.resolve(frame.contentDocument?.fonts?.ready)
              .catch(() => undefined).then(resolve);
          }} catch {{ resolve(); }}
        }};
        frame.addEventListener('load', finish, {{ once: true }});
        frame.addEventListener('error', finish, {{ once: true }});
        try {{
          if (frame.contentWindow?.location.href !== 'about:blank'
              && frame.contentDocument?.readyState === 'complete') finish();
        }} catch {{ finish(); }}
      }});
    }}
    const fontsReady = Promise.all(slides.map(waitForFrame));
    window.cleanDeck = {{ go, step, count: slides.length, fontsReady, provisional: true }};
    go(byNumber.has(current) ? current : ordered[0]);
    Promise.resolve(fontsReady).catch(() => undefined).then(() => requestAnimationFrame(() => {{
      fitDeck();
      dispatchEvent(new CustomEvent('deckfontsready'));
    }}));
  }})();
  </script>"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Live Presentation Preview</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ width:100%; height:100%; margin:0; overflow:hidden; background:#000; }}
    .stage {{ position:fixed; inset:0; overflow:hidden; background:#000; }}
    #deck {{
      position:absolute; width:{canvas_w}px; height:{canvas_h}px;
      transform-origin:0 0; overflow:hidden; background:#000;
    }}
    .provisional-slide {{
      position:absolute; inset:0; width:{canvas_w}px; height:{canvas_h}px;
      display:none; border:0; margin:0; background:#000; overflow:hidden;
    }}
    .provisional-slide.active {{ display:block; }}
  </style>
</head>
<body>
  <div class="stage"><main class="deck" id="deck">
{frames}
  </main></div>
{player}
</body>
</html>"""


@app.get("/api/decks/{deck_id}/file")
def deck_file(deck_id: int, rel: str, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    target = _safe_target(row, rel)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.get("/api/decks/{deck_id}/attachments/{attachment_index}")
def deck_attachment(
    deck_id: int, attachment_index: int, download: int = 0,
    user=Depends(require_user), con=Depends(get_db),
):
    """Safely preview or download one original user-uploaded attachment."""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    try:
        seed = json.loads(row["seed_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        seed = {}
    records = seed.get("attachments") or []
    if attachment_index < 0 or attachment_index >= len(records):
        raise HTTPException(status_code=404, detail="附件不存在")
    record = records[attachment_index]
    if not isinstance(record, dict):
        raise HTTPException(status_code=404, detail="附件不存在")

    upload_root = engine.deck_uploads_dir(deck_id).resolve()
    raw_path = str(record.get("path") or "").strip()
    target = (Path(raw_path) if raw_path else upload_root / str(record.get("stored_name") or "")).resolve()
    try:
        target.relative_to(upload_root)
    except ValueError as error:
        raise HTTPException(status_code=403, detail="附件路径无效") from error
    if not target.is_file():
        raise HTTPException(status_code=404, detail="附件文件不存在")

    display_name = str(record.get("name") or target.name)
    media_type = mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    if Path(display_name).suffix.lower() in {".html", ".htm", ".svg", ".xml"}:
        media_type = "text/plain; charset=utf-8"
    if download:
        return FileResponse(target, media_type=media_type, filename=display_name)
    headers = {
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    # HTML/SVG/XML are deliberately served as plain text and remain sandboxed.
    # Applying CSP `sandbox` to PDFs disables Chromium's built-in PDF viewer,
    # leaving an empty embedded frame even when the PDF itself is valid.
    if media_type.startswith("text/plain"):
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    return FileResponse(target, media_type=media_type, headers=headers)


@app.get("/api/decks/{deck_id}/attachments/{attachment_index}/preview")
def deck_attachment_preview(
    deck_id: int, attachment_index: int,
    user=Depends(require_user), con=Depends(get_db),
):
    """Return the safe, extracted preview produced while ingesting a document."""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    try:
        seed = json.loads(row["seed_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        seed = {}
    records = seed.get("attachments") or []
    if attachment_index < 0 or attachment_index >= len(records):
        raise HTTPException(status_code=404, detail="附件不存在")
    record = records[attachment_index]
    if not isinstance(record, dict):
        raise HTTPException(status_code=404, detail="附件不存在")

    upload_root = engine.deck_uploads_dir(deck_id).resolve()
    assets = []
    for asset_index, image in enumerate(record.get("images") or []):
        if not isinstance(image, dict):
            continue
        raw_path = str(image.get("source_path") or "").strip()
        if not raw_path:
            continue
        target = Path(raw_path).resolve()
        try:
            target.relative_to(upload_root)
        except ValueError:
            continue
        if not target.is_file():
            continue
        assets.append({
            "url": f"/api/decks/{deck_id}/attachments/{attachment_index}/assets/{asset_index}",
            "label": f"第 {image.get('page')} 页" if image.get("page") else target.name,
            "kind": str(image.get("kind") or "image"),
        })
    return {
        "name": str(record.get("name") or record.get("stored_name") or "附件"),
        "type": str(record.get("type") or "file"),
        "text": str(record.get("text") or ""),
        "truncated": bool(record.get("truncated")),
        "notes": [str(note) for note in (record.get("notes") or []) if note],
        "assets": assets,
    }


@app.get("/api/decks/{deck_id}/attachments/{attachment_index}/assets/{asset_index}")
def deck_attachment_preview_asset(
    deck_id: int, attachment_index: int, asset_index: int,
    user=Depends(require_user), con=Depends(get_db),
):
    """Serve one generated page/image preview without exposing arbitrary paths."""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    try:
        seed = json.loads(row["seed_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        seed = {}
    records = seed.get("attachments") or []
    if attachment_index < 0 or attachment_index >= len(records):
        raise HTTPException(status_code=404, detail="附件不存在")
    record = records[attachment_index]
    images = record.get("images") if isinstance(record, dict) else None
    if not isinstance(images, list) or asset_index < 0 or asset_index >= len(images):
        raise HTTPException(status_code=404, detail="预览资源不存在")
    image = images[asset_index]
    raw_path = str(image.get("source_path") or "").strip() if isinstance(image, dict) else ""
    target = Path(raw_path).resolve() if raw_path else Path("/")
    upload_root = engine.deck_uploads_dir(deck_id).resolve()
    try:
        target.relative_to(upload_root)
    except ValueError as error:
        raise HTTPException(status_code=403, detail="预览资源路径无效") from error
    if not target.is_file():
        raise HTTPException(status_code=404, detail="预览资源不存在")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/decks/{deck_id}/asset-thumbnail")
def deck_asset_thumbnail(deck_id: int, rel: str, user=Depends(require_user), con=Depends(get_db)):
    """Return a cached, lightweight preview for Image Agent artifacts."""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    target = _safe_target(row, rel)
    if not os.path.isfile(target) or os.path.splitext(target)[1].lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        stat = os.stat(target)
        cache_key = hashlib.sha1(
            f"{rel}:{stat.st_mtime_ns}:{stat.st_size}:480x300".encode("utf-8")
        ).hexdigest()
        cache_dir = os.path.join(BASE_DIR, "data", "cache", "asset-thumbnails", str(deck_id))
        os.makedirs(cache_dir, exist_ok=True)
        output = os.path.join(cache_dir, cache_key + ".webp")
        if not os.path.isfile(output):
            from PIL import Image, ImageOps
            with Image.open(target) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((480, 300), Image.Resampling.LANCZOS)
                if image.mode in ("RGBA", "LA"):
                    canvas = Image.new("RGB", image.size, (246, 247, 250))
                    canvas.paste(image, mask=image.getchannel("A"))
                    image = canvas
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                temp = output + f".{os.getpid()}.tmp"
                image.save(temp, "WEBP", quality=78, method=4)
                os.replace(temp, output)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail="素材仍在生成，请稍后重试") from error
    return FileResponse(
        output, media_type="image/webp",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# 路径式文件服务:幻灯 HTML 在 iframe 里活渲染时,其相对引用(../base.css、../assets/x.png)
# 会基于本路径解析,因此必须用 /files/<rel:path> 而不是查询参数。
@app.get("/api/decks/{deck_id}/files/{rel:path}")
def deck_file_path(deck_id: int, rel: str, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    target = _safe_target(row, rel)
    if rel == "present.html":
        preview = _static_html_preview_state(row["run_dir"])
        if preview["ready"] and not preview["final"]:
            return HTMLResponse(
                _provisional_present_html(row["run_dir"]),
                headers={"Cache-Control": "no-store", "X-SenseNova-Preview": "provisional"},
            )
    if not os.path.isfile(target):
        if rel == "present.html" and _static_html_preview_state(row["run_dir"])["ready"]:
            return HTMLResponse(
                _provisional_present_html(row["run_dir"]),
                headers={"Cache-Control": "no-store", "X-SenseNova-Preview": "provisional"},
            )
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


# 实时输出流(大小框直播模型输出用):tail runner 日志按 agent 分组
@app.get("/api/decks/{deck_id}/livefeed")
def deck_livefeed(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    payload = trace.livefeed(str(engine.log_path(deck_id)), run_dir=row["run_dir"])
    # A workspace is reused by in-place revisions. Keep this turn's process
    # feed scoped to the Agents that actually appear in its current job log.
    payload["specialist_artifacts"] = trace.scoped_specialist_artifacts(
        row["run_dir"], payload.get("agents", {}).keys()
    )
    return payload


@app.get("/api/decks/{deck_id}/turn-feed")
def deck_turn_feed(
    deck_id: int,
    revision_no: int,
    user=Depends(require_user),
    con=Depends(get_db),
):
    """Return one static conversation turn's archived execution stream.

    In-place revisions keep the Deck id stable.  Immediately before revision
    ``N`` starts, ``jobs`` archives the previous job log under
    ``revision_N/job-before.log``.  Consequently assistant turn ``N - 1`` is
    reconstructed from that immutable log, while the current turn continues to
    use the live job log.  Keeping this endpoint separate from ``progress``
    prevents a complete historical trace from being retransmitted every two
    seconds.
    """
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    current_revision = int(row["revision_no"] or 0)
    if revision_no < 0 or revision_no > current_revision:
        raise HTTPException(status_code=404, detail="修订轮次不存在")

    if revision_no == current_revision:
        payload = trace.livefeed(str(engine.log_path(deck_id)), run_dir=row["run_dir"])
        payload["specialist_artifacts"] = trace.scoped_specialist_artifacts(
            row["run_dir"], payload.get("agents", {}).keys()
        )
        return payload

    archive = (
        Path(row["run_dir"])
        / "_trace"
        / "revisions"
        / f"revision_{revision_no + 1:03d}"
        / "job-before.log"
    )
    if not archive.is_file():
        return {"agents": {}, "page_agents": {}, "specialist_artifacts": {"agents": {}}}
    payload = trace.livefeed(str(archive), run_dir=None)
    artifact_snapshot = archive.parent / "specialist-artifacts.json"
    try:
        payload["specialist_artifacts"] = json.loads(
            artifact_snapshot.read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # Workspaces created before per-turn artifact snapshots can still be
        # reconstructed safely: only retain artifacts for agents that actually
        # occur in this archived log.  This restores old Image Agent galleries
        # without leaking them into a later revision.
        payload["specialist_artifacts"] = trace.scoped_specialist_artifacts(
            row["run_dir"], payload.get("agents", {}).keys()
        )
    return payload


@app.get("/api/decks/{deck_id}/page-history")
def deck_page_history(deck_id: int, n: int, user=Depends(require_user), con=Depends(get_db)):
    """页级详情：真实 Vision 迭代快照，以及 speech.md 对应讲稿。"""
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    if n <= 0:
        raise HTTPException(status_code=400, detail="页码必须大于 0")
    # Multi-round in-place revisions append immutable checks to the same
    # workspace.  Keep enough history to show the initial build and all later
    # revision rounds instead of silently dropping the oldest 24 checks.
    data = trace.page_history(row["run_dir"], n, max_events=100)
    data["speech"] = trace.page_speech(row["run_dir"], n)
    for item in data.get("items", []):
        rel = item.get("image_url") or ""
        item["image_url"] = (
            f"/api/decks/{deck_id}/file?rel={urllib.parse.quote(rel)}"
            if rel else ""
        )
    return data


# 每页上下文:该页的规划 md(渲染成 HTML)+ md 中引用且实际存在的素材图
@app.get("/api/decks/{deck_id}/slideinfo")
def deck_slideinfo(deck_id: int, n: int, user=Depends(require_user), con=Depends(get_db)):
    import re as _re

    from . import mdrender
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    base = row["run_dir"]
    # n=0 = 全局大纲(deck.md);n>=1 = 单页规划
    md_path = os.path.join(base, "plan", "deck.md" if n == 0 else f"slide_{n:02d}.md")
    if not os.path.isfile(md_path):
        return {"exists": False}
    text = open(md_path, encoding="utf-8", errors="replace").read()
    assets = sorted({
        a for a in _re.findall(r"assets/[A-Za-z0-9_\-.]+\.(?:png|jpe?g|webp|gif)", text)
        if os.path.isfile(os.path.join(base, a))
    })
    html_rel = None
    if n >= 1 and os.path.isfile(os.path.join(base, f"slides/slide_{n:02d}.html")):
        html_rel = f"slides/slide_{n:02d}.html"
    elif n == 0 and os.path.isfile(os.path.join(base, "base.css")):
        html_rel = "base.css"
    return {"exists": True, "html": mdrender.render(text), "assets": assets, "html_rel": html_rel}


def _ensure_deck_pptx(row):
    from . import pptx_export
    deck_id = row["id"]
    base = row["run_dir"]
    out = os.path.join(base, f"deck_{deck_id}.pptx")
    pngs = pptx_export.renders_of(base)
    if not pngs:
        raise HTTPException(status_code=409, detail="该 deck 没有成稿图")
    newest = max(os.path.getmtime(p) for p in pngs)
    if not os.path.exists(out) or os.path.getmtime(out) < newest:   # 缓存:渲染图没变就复用
        pptx_export.build_pptx(base, out)
    return out


@app.get("/api/decks/{deck_id}/pptx")
def deck_pptx(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="deck 尚未完成")
    out = _ensure_deck_pptx(row)
    return FileResponse(
        out, filename=f"presentation_{deck_id}.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


def _archive_name(index, title):
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title or "presentation").strip(" ._")
    return f"{int(index or 0):03d}_{(title or 'presentation')[:80]}.pptx"


@app.get("/api/batches/{batch_id}/download")
def batch_download(batch_id: int, user=Depends(require_user), con=Depends(get_db)):
    batch = _own_batch(con, batch_id, user["id"])
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")
    payload = _batch_payload(con, batch, include_decks=True)
    if not payload["terminal"]:
        raise HTTPException(status_code=409, detail="批次仍在生成，全部结束后才能下载最终产物")
    completed = con.execute(
        "SELECT * FROM decks WHERE batch_id = ? AND user_id = ? AND status = 'completed' "
        "ORDER BY batch_index,id",
        (batch_id, user["id"]),
    ).fetchall()
    if not completed:
        raise HTTPException(status_code=409, detail="该批次没有成功生成的 PPT 产物")

    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    archive = BATCHES_DIR / f"batch_{batch_id}.zip"
    tmp_file = tempfile.NamedTemporaryFile(
        dir=BATCHES_DIR, prefix=f".batch_{batch_id}.", suffix=".tmp", delete=False
    )
    tmp = Path(tmp_file.name)
    tmp_file.close()
    manifest = {
        "batch_id": batch_id,
        "name": batch["name"],
        "source_name": batch["source_name"],
        "model": batch["model"],
        "skill_version": batch["skill_version"],
        "status": payload["status"],
        "counts": payload["counts"],
        "decks": [
            {
                "index": deck["batch_index"],
                "deck_id": deck["id"],
                "title": deck["title"],
                "status": deck["status"],
                "error": deck["error"],
            }
            for deck in payload["decks"]
        ],
    }
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive_file:
            archive_file.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for deck in completed:
                pptx = _ensure_deck_pptx(deck)
                archive_file.write(pptx, _archive_name(deck["batch_index"], deck["title"]))
        os.replace(tmp, archive)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return FileResponse(archive, filename=f"batch_{batch_id}_presentations.zip",
                        media_type="application/zip")


@app.get("/api/decks/{deck_id}/download")
def deck_download(deck_id: int, user=Depends(require_user), con=Depends(get_db)):
    row = _own_deck(con, deck_id, user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="deck 不存在")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="deck 尚未完成")
    base = row["run_dir"]
    seed = json.loads(row["seed_json"] or "{}")
    delivery_error = jobs._ensure_static_delivery(row, Path(base))
    if delivery_error:
        raise HTTPException(status_code=409, detail=delivery_error)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Root delivery files are part of the presentation, not implementation
        # metadata.  In particular present.html is the canonical portable
        # player and must travel with slides/ plus the subset fonts in assets/.
        for name in (
            "present.html", "deck.html", "deck_manifest.json", "task_pack.json",
            "info_pack.json", "outline.md", "base.css", "speech.md",
        ):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                z.write(path, name)
        for sub in ("renders", "shots", "slides", "pages", "plan", "research", "assets"):
            d = os.path.join(base, sub)
            for root, _, files in os.walk(d):
                for fn in files:
                    fp = os.path.join(root, fn)
                    z.write(fp, os.path.relpath(fp, base))
    buf.seek(0)
    return Response(
        buf.read(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="deck_{deck_id}.zip"'},
    )
