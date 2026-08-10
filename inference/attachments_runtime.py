"""Runtime staging for Studio attachments inside an engine workspace."""
import base64
import io
import json
import os
import shutil
from pathlib import Path

MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1536"))
MAX_ATTACHMENT_IMAGE_BLOCKS = int(os.environ.get("MAX_ATTACHMENT_IMAGE_BLOCKS", "0"))


def _safe_rel(rel: str) -> Path | None:
    if not rel:
        return None
    rel_path = Path(str(rel).replace("\\", "/"))
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return None
    if not rel_path.parts or rel_path.parts[0] != "attachments":
        return None
    return rel_path


def _stage_items(root: Path, items: list, default_kind: str) -> list[dict]:
    staged = []
    for item in items:
        if not isinstance(item, dict):
            continue
        src = item.get("source_path")
        rel = _safe_rel(item.get("workspace_rel"))
        if not src or rel is None:
            continue
        src_path = Path(src)
        if not src_path.is_file():
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst)
        staged.append({
            "name": item.get("name", ""),
            "stored_name": item.get("stored_name", ""),
            "type": item.get("type", ""),
            "size": item.get("size", 0),
            "kind": item.get("kind", default_kind),
            "page": item.get("page"),
            "path": str(rel),
        })
    return staged


def _encode_image(path: Path) -> dict | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = MAX_VISION_EDGE / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
    except Exception:
        return None
    return {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(data).decode("ascii"),
    }


def _message_images(root: Path, staged: list[dict]) -> list[dict]:
    out = []
    items = staged[:MAX_ATTACHMENT_IMAGE_BLOCKS] if MAX_ATTACHMENT_IMAGE_BLOCKS > 0 else staged
    for item in items:
        rel = _safe_rel(item.get("path"))
        if rel is None:
            continue
        source = _encode_image(root / rel)
        if not source:
            continue
        out.append({
            "name": item.get("name", ""),
            "kind": item.get("kind", "image"),
            "page": item.get("page"),
            "path": str(rel),
            "source": source,
        })
    return out


def build_initial_user_content(seed, brief):
    """Return Anthropic-compatible initial user content with attachment images inline."""
    images = seed.get("_attachment_message_images") if isinstance(seed, dict) else None
    if not images:
        return brief
    blocks = []
    if isinstance(brief, list):
        blocks.extend(brief)
    else:
        blocks.append({"type": "text", "text": str(brief)})
    blocks.append({
        "type": "text",
        "text": (
            "\n\n【附件图片】以下图片来自用户上传附件,已经作为 image block "
            "直接附在本条模型消息中;请直接理解图片内容,不要再要求调用 vision_analyze。"
        ),
    })
    for idx, item in enumerate(images, 1):
        label = item.get("name") or item.get("path") or "attachment image"
        loc = item.get("path", "")
        kind = item.get("kind", "image")
        page = item.get("page")
        page_text = f"; page/slide/sheet: {page}" if page else ""
        blocks.append({"type": "text", "text": f"附件图片 {idx}: {label}; kind: {kind}; path: {loc}{page_text}"})
        blocks.append({"type": "image", "source": item["source"]})
    return blocks


def stage_seed_attachments(seed, run_dir) -> int:
    """Copy Studio attachments into run_dir/attachments.

    In web_parse mode, text extracted from attachments is already appended to
    seed["query"], and image/scanned-PDF pages are staged for vision_analyze.
    In pipeline_agent mode, raw files are staged under attachments/raw/ so an
    attachment_reader subagent can parse them inside the engine sandbox.
    """
    if not isinstance(seed, dict):
        return 0

    root = Path(run_dir)
    raw_items = seed.get("_raw_attachments") or []
    image_items = seed.get("_attachment_images") or []
    raw = _stage_items(root, raw_items if isinstance(raw_items, list) else [], "raw")
    images = _stage_items(root, image_items if isinstance(image_items, list) else [], "image")
    message_images = _message_images(root, images)
    if message_images:
        seed["_attachment_message_images"] = message_images

    if raw or images:
        manifest = root / "attachments" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "mode": seed.get("attachment_mode") or "web_parse",
            "raw_attachments": raw,
            "images": images,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(raw) + len(images)
