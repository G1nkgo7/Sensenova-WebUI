"""Short, user-facing titles for presentation conversations.

The sidebar must not expose a truncated copy of a long prompt.  A deterministic
fallback is returned immediately, while a tiny model call may refine it in the
background without delaying deck generation.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from collections.abc import Callable


_REQUEST_PREFIX = re.compile(
    r"^(?:请|麻烦|希望|想要|我想|我们需要|我需要|请帮我|帮我|给我|做个|做一个|制作一个|制作一份|做一份)+"
)
_PAGE_HINT = re.compile(r"(?:大约|约|左右)?\s*\d+\s*页(?:左右)?(?:的)?", re.I)
_PPT_WORD = re.compile(r"(?:演示文稿|幻灯片|ppt|PPT)")
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.I)


def _query_core(query: str) -> str:
    text = str(query or "").strip()
    for marker in ("\n\n本次演示偏好：", "\n\n附件处理要求：", "\n\n已上传附件："):
        text = text.split(marker, 1)[0]
    return re.sub(r"\s+", " ", text).strip()


def _trim_title(value: str, limit: int = 30) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = re.sub(r"^(?:标题|任务标题|演示标题)\s*[:：]\s*", "", value)
    value = value.strip(" \t\r\n\"'“”‘’。，、；;：:")
    if len(value) <= limit:
        return value
    cut = value[:limit].rstrip(" ，、；;：:")
    # Do not leave an unmatched Chinese book-title bracket in the sidebar.
    if cut.count("《") > cut.count("》") and "》" in value[limit:]:
        end = value.find("》", limit)
        if end < limit + 10:
            cut = value[: end + 1]
    return cut.rstrip() + "…"


def fallback_title(query: str) -> str:
    """Produce an immediate readable title without a network dependency."""
    text = _query_core(query)
    if not text:
        return "未命名演示"

    work = re.search(r"《[^》]{1,36}》", text)
    if work:
        prefix = text[: work.start()]
        prefix = re.split(r"[，。！？；,:;]", prefix)[-1]
        prefix = re.sub(r"^.*?(?:围绕|聚焦|关于|解读|分析)", "", prefix)
        prefix = _REQUEST_PREFIX.sub("", prefix)
        prefix = _PPT_WORD.sub("", _PAGE_HINT.sub("", prefix))
        prefix = re.sub(r"[^\u3400-\u9fffA-Za-z0-9·\-]", "", prefix)[-8:]
        intent = "PPT分析" if any(word in text for word in ("分析", "拆解", "讨论", "对比", "研究")) else "主题PPT"
        return _trim_title(f"{prefix}{work.group(0)}{intent}")

    intro = re.search(r"介绍\s*([^，。；]{2,30}?)\s*(?:的)?(?:PPT|ppt|演示文稿|幻灯片)", text)
    if intro:
        return _trim_title(f"{intro.group(1).strip()}介绍PPT")
    about = re.search(r"(?:关于|围绕|聚焦)\s*([^，。；]{2,32}?)\s*(?:的)?(?:PPT|ppt|演示文稿|幻灯片)", text)
    if about:
        return _trim_title(f"{about.group(1).strip()}PPT")

    first = re.split(r"[。！？；\n]", text, 1)[0]
    first = _REQUEST_PREFIX.sub("", first)
    first = _PAGE_HINT.sub("", first)
    first = re.sub(r"^(?:关于|围绕|聚焦)", "", first)
    first = re.sub(r"(?:制作|生成|设计)(?:一份|一个)?", "", first)
    first = re.sub(r"\s+", " ", first).strip(" ，、；;：:")
    if not _PPT_WORD.search(first):
        first += " PPT"
    return _trim_title(first) or "未命名演示"


def display_title(stored_title: str, query: str) -> str:
    """Keep curated/revision titles; replace legacy raw-query prefixes."""
    stored = str(stored_title or "").strip()
    core = _query_core(query)
    looks_raw = bool(core and stored and (core.startswith(stored) or stored.startswith(core[: min(24, len(core))])))
    generic = bool(re.match(r"^(?:请|帮我|给我|做个|做一个|做一份|制作|我们需要|我想)", stored))
    return fallback_title(core) if not stored or looks_raw or generic else stored


def _agent_content(payload: dict) -> str:
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return str(content)


def summarize_with_agent(query: str, model: dict, timeout: float = 12.0) -> str:
    """Ask the selected OpenAI-compatible model for one compact title."""
    fallback = fallback_title(query)
    base_url = str(model.get("url") or model.get("base_url") or "").rstrip("/")
    model_id = str(model.get("model") or model.get("engine_model") or "").strip()
    if not base_url or not model_id:
        return fallback
    body = {
        "model": model_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是演示任务标题编辑。根据用户需求生成历史记录标题。只输出标题，不解释；"
                    "中文不超过18个汉字，英文不超过8个词；保留核心主题、人物或作品名；"
                    "不要以‘帮我’‘制作一份’开头，不写页数、风格、受众等次要要求。"
                    "示例：围绕周星驰《功夫女足》展开分析 → 周星驰《功夫女足》PPT分析"
                ),
            },
            {"role": "user", "content": _query_core(query)},
        ],
        "max_tokens": 64,
        "temperature": 0.1,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    api_key = str(model.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = _agent_content(json.loads(response.read().decode("utf-8")))
    except Exception:  # Title refinement must never affect deck creation.
        return fallback
    content = _THINK_BLOCK.sub("", content).strip()
    if content.startswith("{"):
        try:
            content = json.loads(content).get("title", "")
        except (json.JSONDecodeError, AttributeError):
            pass
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = _trim_title(lines[-1] if lines else "")
    if not title or len(title) > 34 or re.match(r"^(?:好的|当然|以下|根据)", title):
        return fallback
    return title


def schedule_summary(query: str, model: dict, apply: Callable[[str], None]) -> str:
    """Return a fallback now and refine it asynchronously."""
    immediate = fallback_title(query)

    def run():
        title = summarize_with_agent(query, model)
        try:
            apply(title)
        except Exception:
            pass

    threading.Thread(target=run, name="studio-title-summary", daemon=True).start()
    return immediate
