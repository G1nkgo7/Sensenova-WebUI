"""Validate and store user-authorized custom fonts for static presentations."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile
from fontTools.ttLib import TTFont

from . import engine


MAX_FONT_FILES = 6
MAX_FONT_BYTES = 25 * 1024 * 1024
FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}
FONT_ROLES = {"title", "body", "number", "annotation"}
BUILTIN_FAMILIES = {
    "Noto Sans SC", "Noto Serif SC", "LXGW WenKai", "Smiley Sans",
    "Ma Shan Zheng", "ZCOOL KuaiLe", "ZCOOL XiaoWei", "Zhi Mang Xing",
    "Archivo", "Fraunces", "Spectral", "Montserrat", "Oswald",
    "Bebas Neue", "Space Grotesk", "Playfair Display", "DM Sans",
    "Manrope", "Sora", "IBM Plex Mono", "Dancing Script", "Kalam",
}


def _safe_name(value: str) -> str:
    name = Path(value or "font").name.replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._()\- \u4e00-\u9fff]+", "_", name).strip()
    return name[:120] or "font"


def _unique_path(directory: Path, name: str) -> Path:
    target = directory / _safe_name(name)
    stem, suffix = target.stem, target.suffix
    index = 2
    while target.exists():
        target = directory / f"{stem}_{index}{suffix}"
        index += 1
    return target


def _font_name(font: TTFont, name_id: int) -> str:
    value = font["name"].getDebugName(name_id)
    return (value or "").strip()


def _inspect_font(path: Path) -> dict:
    try:
        font = TTFont(str(path), lazy=False)
        try:
            family = _font_name(font, 16) or _font_name(font, 1) or path.stem
            subfamily = _font_name(font, 17) or _font_name(font, 2) or "Regular"
            codepoints: set[int] = set()
            for table in font["cmap"].tables:
                codepoints.update(table.cmap)
            os2 = font.get("OS/2")
            weight = int(getattr(os2, "usWeightClass", 400) or 400)
            return {
                "family": family[:160],
                "subfamily": subfamily[:80],
                "weight": max(1, min(weight, 1000)),
                "glyph_count": len(codepoints),
                "has_latin": any(0x41 <= item <= 0x7A for item in codepoints),
                "has_cjk": any(0x3400 <= item <= 0x9FFF for item in codepoints),
            }
        finally:
            font.close()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"无法读取字体「{path.name}」：{exc}") from exc


def parse_role_config(raw: str) -> dict[str, str]:
    if not (raw or "").strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="字体角色配置不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="字体角色配置必须是对象")
    result: dict[str, str] = {}
    for role, selection in payload.items():
        if role not in FONT_ROLES or not isinstance(selection, str):
            continue
        selection = selection.strip()
        if selection and selection != "auto":
            result[role] = selection
    return result


def store_custom_fonts(
    deck_id: int,
    files: list[UploadFile],
    roles_raw: str,
    license_acknowledged: bool,
) -> dict | None:
    roles = parse_role_config(roles_raw)
    uploads = [item for item in (files or []) if getattr(item, "filename", "")]
    if not uploads and not roles:
        return None
    if len(uploads) > MAX_FONT_FILES:
        raise HTTPException(status_code=400, detail=f"自定义字体最多上传 {MAX_FONT_FILES} 个")
    if uploads and not license_acknowledged:
        raise HTTPException(status_code=400, detail="上传字体前请确认你拥有演示与嵌入使用权")

    directory = engine.deck_uploads_dir(deck_id) / "fonts"
    directory.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    original_to_id: dict[str, str] = {}
    for index, upload in enumerate(uploads, 1):
        original = _safe_name(upload.filename)
        suffix = Path(original).suffix.lower()
        if suffix not in FONT_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不支持的字体格式：{original}")
        data = upload.file.read()
        if not data:
            raise HTTPException(status_code=400, detail=f"字体文件为空：{original}")
        if len(data) > MAX_FONT_BYTES:
            raise HTTPException(status_code=400, detail=f"字体「{original}」超过 25MB")
        path = _unique_path(directory, original)
        path.write_bytes(data)
        metadata = _inspect_font(path)
        font_id = f"font-{index:02d}-{hashlib.sha256(data).hexdigest()[:10]}"
        record = {
            "id": font_id,
            "name": original,
            "stored_name": path.name,
            "path": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "user_authorized": True,
            **metadata,
        }
        records.append(record)
        original_to_id[original.casefold()] = font_id

    normalized_roles: dict[str, dict[str, str]] = {}
    for role, selection in roles.items():
        if selection.startswith("builtin:"):
            family = selection.removeprefix("builtin:").strip()
            if family not in BUILTIN_FAMILIES:
                raise HTTPException(status_code=400, detail=f"未批准的内置字体：{family}")
            normalized_roles[role] = {"kind": "builtin", "family": family}
        elif selection.startswith("custom:"):
            name = _safe_name(selection.removeprefix("custom:")).casefold()
            font_id = original_to_id.get(name)
            if not font_id:
                raise HTTPException(status_code=400, detail=f"字体角色引用了未上传文件：{name}")
            normalized_roles[role] = {"kind": "custom", "font_id": font_id}
        else:
            raise HTTPException(status_code=400, detail=f"无效字体角色值：{selection}")

    return {
        "version": 1,
        "license_acknowledged": bool(license_acknowledged),
        "roles": normalized_roles,
        "fonts": records,
    }
