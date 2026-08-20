"""FastAPI adapter for the vendored dazzle dynamic-deck runtime."""
from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dynamic.viz import agent_runtime as runtime
except ModuleNotFoundError:
    # The V1 distributable intentionally omits the dynamic presentation
    # runtime. Model configuration helpers in this module remain importable;
    # dynamic routes are never registered while STUDIO_DYNAMIC_ENABLED=0.
    runtime = None

from . import auth, custom_models, engine, features, service_config, titles, trace
from .db import connect

router = APIRouter()
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}
_DYNAMIC_EXPORT_EXCLUDED_DIRS = {"_trace", "shots", "__pycache__"}
_DYNAMIC_EXPORT_EXCLUDED_FILES = {"events.jsonl", "messages.json", "meta.json"}
_DYNAMIC_EXPORT_EXCLUDED_SUFFIXES = {".py", ".pyc", ".log"}


def _get_db():
    con = connect()
    try:
        yield con
    finally:
        con.close()


def _current_user(request: Request, con=Depends(_get_db)):
    return features.user_for_request(request, con, auth)


def _require_user(user=Depends(_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _owner(user) -> str:
    return f"{user['id']}:{user['username']}"


def _is_admin(user) -> bool:
    return user["role"] == "admin"


def _model_registry(user=None) -> dict[str, dict]:
    """Reuse the Web Demo model registry and its gitignored credentials."""
    env = engine._read_env_file(engine.INFERENCE_DIR / ".env")
    out = {}
    for key, model in engine.user_selectable_models():
        if model.get("backend") != "openai":
            continue
        api_key = model.get("api_key", "") or env.get(model.get("api_key_env", ""), "")
        # Authenticated endpoints without a configured key are not selectable.
        if model.get("api_key_env") and not api_key:
            continue
        out[key] = {
            "label": model.get("label") or key,
            "url": engine.resolve_base_url(model),
            "model": model.get("engine_model") or key,
            "api_key": api_key,
            "api_style": "openai",
            "thinking_transport": bool(
                engine.thinking_capability(key).get("toggle")
            ),
            "img_mode": "openai_url",
            "custom": False,
        }
    if user:
        con = connect()
        try:
            rows = custom_models.list_rows(con, user["id"])
            for row in rows:
                cfg = custom_models.runtime_config(row)
                key = custom_models.key_for(row["id"])
                # 纯文本自定义模型（vision_enabled=0，如 DeepSeek V4）→ img_mode="text"：
                # runtime 撤下 vision_analyze + 发送时丢弃图像块，避免 image_url 触发端点 400。
                out[key] = {
                    "label": cfg["label"],
                    "url": cfg["base_url"],
                    "model": cfg["engine_model"],
                    "api_key": cfg["api_key"],
                    "api_style": "openai",
                    "thinking_transport": False,
                    "img_mode": "openai_url" if cfg.get("multimodal") else "text",
                    "custom": True,
                }
        finally:
            con.close()
    return out


def _model_ok(key: str, model: dict) -> bool:
    now = time.time()
    cached = _HEALTH_CACHE.get(key)
    if cached and now - cached[0] < 30:
        return cached[1]
    ok = False
    try:
        req = urllib.request.Request(model["url"].rstrip("/") + "/models")
        if model.get("api_key"):
            req.add_header("Authorization", "Bearer " + model["api_key"])
        with urllib.request.urlopen(req, timeout=4) as response:
            ok = response.status == 200
    except (OSError, urllib.error.URLError):
        ok = False
    _HEALTH_CACHE[key] = (now, ok)
    return ok


def _models_payload(user=None):
    models = _model_registry(user)
    return [
        {"key": key, "label": value["label"],
         # 自定义 OpenAI 兼容端点未必开放 /models；不以该探活结果阻塞生成。
         "ok": None if value.get("custom") else _model_ok(key, value),
         "custom": bool(value.get("custom")),
         "thinking_toggle": bool(value.get("thinking_transport"))}
        for key, value in models.items()
    ]


def _can_access(conv_id: str, user) -> bool:
    return _is_admin(user) or runtime.conv_owner(conv_id) == _owner(user)


def _valid_conv(conv_id: str) -> bool:
    return bool(conv_id and _SAFE_ID.fullmatch(conv_id))


def _dynamic_delivery_files(base: Path):
    """Yield portable dynamic-deck files while excluding runtime/debug state."""
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part in _DYNAMIC_EXPORT_EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.name in _DYNAMIC_EXPORT_EXCLUDED_FILES:
            continue
        if path.suffix.lower() in _DYNAMIC_EXPORT_EXCLUDED_SUFFIXES:
            continue
        yield path, rel


@router.get("/dynamic")
def dynamic_page(request: Request, user=Depends(_current_user)):
    # Dynamic and static now share the same composer and editor shell.
    return RedirectResponse("/?mode=dynamic", status_code=303)


@router.get("/api/dynamic/conversations")
def conversations(user=Depends(_require_user), con=Depends(_get_db)):
    registry = _model_registry(user)
    default_model = engine.DEFAULT_MODEL if engine.DEFAULT_MODEL in registry else next(iter(registry), "")
    items = runtime.list_conversations(_owner(user), _is_admin(user))
    prefs = {
        row["item_id"]: bool(row["pinned"])
        for row in con.execute(
            "SELECT item_id,pinned FROM history_preferences "
            "WHERE user_id = ? AND item_kind = 'dynamic'",
            (user["id"],),
        ).fetchall()
    }
    for item in items:
        meta = runtime._read_meta(item["conv_id"])
        item["display_title"] = titles.display_title(item.get("title"), meta.get("user_query") or "")
        item["pinned"] = prefs.get(str(item["conv_id"]), False)
    return {
        "items": items,
        "studio": True,
        "user": user["username"],
        "is_admin": _is_admin(user),
        "models": _models_payload(user),
        "default_model": default_model,
    }


@router.get("/api/dynamic/conversation")
def conversation(conv_id: str, user=Depends(_require_user)):
    if not _valid_conv(conv_id) or not runtime._conv_dir(conv_id).is_dir():
        raise HTTPException(status_code=404, detail="会话不存在")
    if not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return {
        "conv_id": conv_id,
        "meta": runtime._read_meta(conv_id),
        "events": runtime.read_events(conv_id),
        "active": runtime.conversation_active(conv_id),
    }


@router.get("/api/dynamic/output-location")
def output_location(conv_id: str, user=Depends(_require_user)):
    """Return the owned dynamic conversation directory for local workflows."""
    if not _valid_conv(conv_id) or not runtime._conv_dir(conv_id).is_dir():
        raise HTTPException(status_code=404, detail="会话不存在")
    if not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return {"path": str(runtime._conv_dir(conv_id).resolve())}


@router.get("/api/dynamic/download")
def download(conv_id: str, user=Depends(_require_user)):
    """Download deck.html together with every local asset needed for playback."""
    if not _valid_conv(conv_id) or not runtime._conv_dir(conv_id).is_dir():
        raise HTTPException(status_code=404, detail="会话不存在")
    if not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    base = runtime._conv_dir(conv_id).resolve()
    if not (base / "deck.html").is_file():
        raise HTTPException(status_code=409, detail="动态演示尚未生成完成")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path, rel in _dynamic_delivery_files(base):
            archive.write(path, rel.as_posix())
    buf.seek(0)
    return Response(
        buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="dynamic_{conv_id}.zip"'},
    )


@router.get("/api/dynamic/stream")
async def stream(conv_id: str, user=Depends(_require_user)):
    if not _valid_conv(conv_id) or not runtime._conv_dir(conv_id).is_dir():
        raise HTTPException(status_code=404, detail="会话不存在")
    if not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    event_path = runtime._events_path(conv_id)

    # A conversation may be continued after an earlier `done` event. Start the
    # SSE cursor at the latest user turn so an old terminal event cannot close
    # the new stream before continuation events arrive.
    start_pos = 0
    if event_path.exists():
        try:
            with event_path.open("r", encoding="utf-8") as handle:
                while True:
                    line_pos = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        if json.loads(line).get("kind") == "user":
                            start_pos = line_pos
                    except json.JSONDecodeError:
                        continue
        except OSError:
            start_pos = 0

    async def events():
        pos = start_pos
        buf = ""
        idle = 0
        finished = False
        while not finished:
            data = ""
            if event_path.exists():
                with event_path.open("r", encoding="utf-8") as handle:
                    handle.seek(pos)
                    data = handle.read()
                    pos = handle.tell()
            if data:
                idle = 0
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    yield f"data: {line}\n\n"
                    try:
                        finished = json.loads(line).get("kind") in ("done", "error")
                    except json.JSONDecodeError:
                        pass
                    if finished:
                        break
            else:
                if not runtime.conversation_active(conv_id):
                    idle += 1
                    if idle >= 3:
                        break
                yield ": ping\n\n"
                await asyncio.sleep(0.4)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    })


@router.post("/api/dynamic/send")
async def send(request: Request, user=Depends(_require_user)):
    body = await request.json()
    conv_id = (body.get("conv_id") or "").strip() or None
    if conv_id and not _valid_conv(conv_id):
        raise HTTPException(status_code=400, detail="非法 conv_id")
    continuing = bool(conv_id and runtime._conv_dir(conv_id).is_dir())
    if continuing and not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权操作该会话")

    registry = _model_registry(user)
    default_model = engine.DEFAULT_MODEL if engine.DEFAULT_MODEL in registry else next(iter(registry), "")
    model_key = (
        runtime._read_meta(conv_id).get("model_key") if continuing else body.get("model")
    ) or default_model
    model = registry.get(model_key)
    if not model:
        raise HTTPException(status_code=503, detail="动态模型未配置")
    existing_meta = runtime._read_meta(conv_id) if continuing else {}
    raw_thinking = body.get("thinking") if "thinking" in body else existing_meta.get("requested_thinking", True)
    requested_thinking = raw_thinking is True or str(raw_thinking).strip().lower() in {"1", "true", "yes", "on"}
    thinking_transport = bool(model.get("thinking_transport"))
    effective_thinking = requested_thinking and thinking_transport
    message = (body.get("message") or "").strip()
    user_message = message
    style = (body.get("style") if "style" in body else existing_meta.get("style", "") or "").strip()
    theme = (body.get("theme") if "theme" in body else existing_meta.get("theme", "") or "").strip()
    scheme = (body.get("scheme") if "scheme" in body else existing_meta.get("scheme", "") or "").strip()
    try:
        raw_slide_count = body.get("slide_count") if "slide_count" in body else existing_meta.get("slide_count", 0)
        slide_count = max(0, min(18, int(raw_slide_count or 0)))
    except (TypeError, ValueError):
        slide_count = 0
    generation_preferences = {
        key: value for key, value in {
            "page_count": slide_count,
            "content_theme": theme,
            "visual_style": style,
            "color_scheme": scheme,
        }.items() if value not in ("", 0, None)
    }

    try:
        shared_env = engine._read_env_file(engine.INFERENCE_DIR / ".env")
        services = service_config.runtime_payload(user["id"])
        generation_limits = services["generation"]
        image_service = services.get("image_generation")
        # 动态 StudioAgent 直读进程 env 的 OPENAI_API_KEY/IMAGE_MODEL、SERPER_API_KEY（由 launch.py
        # 从 .env 注入）。故 gate 也认这几个来源：per-user 配置 > inference/.env > 部署进程 env。
        # 否则凭证在部署 env 里齐全、工具却被 gate 撤下（enable_image_gen=False）。
        _deploy_image_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SENSENOVA_IMAGE_API_KEY")
        enable_image_gen = bool(image_service) or bool(
            shared_env.get("IMAGE_API_KEY") or shared_env.get("OPENAI_API_KEY")
        ) or (os.environ.get("ENABLE_IMAGE_GEN", "1") != "0" and bool(_deploy_image_key))
        new_id = runtime.send_message(
            conv_id,
            message,
            model["url"],
            model["model"],
            enable_image_gen=enable_image_gen,
            model_key=model_key,
            model_label=model["label"],
            img_mode=model["img_mode"],
            owner=_owner(user),
            api_key=model["api_key"],
            api_style=model["api_style"],
            thinking=effective_thinking,
            thinking_transport=thinking_transport,
            requested_thinking=requested_thinking,
            generation_preferences=generation_preferences,
            tool_config=services,
            max_tokens=generation_limits["max_tokens"],
            max_turns=generation_limits["dynamic_max_turns"],
        )
        meta = runtime._read_meta(new_id)
        if not continuing:
            meta["user_query"] = user_message
            meta["title"] = titles.fallback_title(user_message)
        meta.update({
            "slide_count": slide_count,
            "theme": theme,
            "style": style,
            "scheme": scheme,
            "thinking": effective_thinking,
            "requested_thinking": requested_thinking,
            "effective_thinking": effective_thinking,
            "thinking_transport": "chat_template_kwargs" if thinking_transport else "",
            "generation_preferences": generation_preferences,
            "runtime_limits": generation_limits,
            "skill_version": "dazzle-deck",
        })
        runtime._write_meta(new_id, meta)
        if not continuing:
            titles.schedule_summary(
                user_message, model,
                lambda title: runtime.update_meta_fields(new_id, title=title),
            )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "conv_id": new_id}


@router.post("/api/dynamic/regenerate")
async def regenerate(request: Request, user=Depends(_require_user)):
    """Start a fresh dynamic conversation from one completed history item."""
    body = await request.json()
    source_id = str(body.get("conv_id") or "").strip()
    if not _valid_conv(source_id) or not runtime._conv_dir(source_id).is_dir():
        raise HTTPException(status_code=404, detail="会话不存在")
    if not _can_access(source_id, user):
        raise HTTPException(status_code=403, detail="无权操作该会话")
    source_meta = runtime._read_meta(source_id)
    if source_meta.get("status") != "completed" or runtime.conversation_active(source_id):
        raise HTTPException(status_code=409, detail="仅已完成的演示可以再次生成")
    query = str(source_meta.get("user_query") or "").strip()
    if not query:
        raise HTTPException(status_code=409, detail="原任务缺少生成需求，无法再次生成")

    registry = _model_registry(user)
    model_key = source_meta.get("model_key") or engine.DEFAULT_MODEL
    model = registry.get(model_key)
    if not model:
        raise HTTPException(status_code=409, detail="原任务使用的模型已下线")
    services = service_config.runtime_payload(user["id"])
    generation_limits = services["generation"]
    image_service = services.get("image_generation")
    shared_env = engine._read_env_file(engine.INFERENCE_DIR / ".env")
    requested_thinking = bool(source_meta.get("requested_thinking", True))
    thinking_transport = bool(model.get("thinking_transport"))
    effective_thinking = requested_thinking and thinking_transport
    preferences = dict(source_meta.get("generation_preferences") or {})
    new_id = runtime.send_message(
        None, query, model["url"], model["model"],
        enable_image_gen=bool(image_service) or bool(
            shared_env.get("IMAGE_API_KEY") or shared_env.get("OPENAI_API_KEY")
        ),
        model_key=model_key, model_label=model["label"], img_mode=model["img_mode"],
        owner=_owner(user), api_key=model["api_key"], api_style=model["api_style"],
        thinking=effective_thinking, thinking_transport=thinking_transport,
        requested_thinking=requested_thinking, generation_preferences=preferences,
        tool_config=services, max_tokens=generation_limits["max_tokens"],
        max_turns=generation_limits["dynamic_max_turns"],
    )
    meta = runtime._read_meta(new_id)
    meta.update({
        "title": source_meta.get("title") or titles.fallback_title(query),
        "user_query": query,
        "slide_count": source_meta.get("slide_count", 0),
        "theme": source_meta.get("theme", ""),
        "style": source_meta.get("style", ""),
        "scheme": source_meta.get("scheme", ""),
        "skill_version": source_meta.get("skill_version", "dazzle-deck"),
    })
    runtime._write_meta(new_id, meta)
    return {"ok": True, "conv_id": new_id, "source_conv_id": source_id}


@router.post("/api/dynamic/stop")
async def stop(request: Request, user=Depends(_require_user)):
    body = await request.json()
    conv_id = (body.get("conv_id") or "").strip()
    if not _valid_conv(conv_id) or not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权操作该会话")
    runtime.stop_conversation(conv_id)
    return {"ok": True}


@router.get("/api/dynamic/page-speech")
def page_speech(conv_id: str, n: int, user=Depends(_require_user)):
    """Return the current page's notes for dynamic HTML decks as well."""
    if not _valid_conv(conv_id) or not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权访问该会话")
    if n <= 0:
        raise HTTPException(status_code=400, detail="页码必须大于 0")
    return trace.page_speech(str(runtime._conv_dir(conv_id)), n)


@router.post("/api/dynamic/delete")
async def delete(request: Request, user=Depends(_require_user), con=Depends(_get_db)):
    body = await request.json()
    conv_id = (body.get("conv_id") or "").strip()
    if not _valid_conv(conv_id) or not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权删除该会话")
    result = runtime.delete_conversation(conv_id)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    con.execute(
        "DELETE FROM history_preferences WHERE user_id = ? AND item_kind = 'dynamic' AND item_id = ?",
        (user["id"], conv_id),
    )
    con.commit()
    return result


@router.get("/dynamic/files/{conv_id}/{rel_path:path}")
def dynamic_file(conv_id: str, rel_path: str, user=Depends(_require_user)):
    if not _valid_conv(conv_id) or not _can_access(conv_id, user):
        raise HTTPException(status_code=403, detail="无权访问该会话资源")
    base = runtime._conv_dir(conv_id).resolve()
    target = (base / rel_path).resolve()
    if (target != base and base not in target.parents) or not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0])
