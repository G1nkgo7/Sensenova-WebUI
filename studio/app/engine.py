"""Bridge from Studio to the presentation inference runtime.

Builds the per-deck job spec and the subprocess command. The engine runs under
inference's own uv env (it needs anthropic/playwright), as a separate OS
process per deck (isolation: cwd / playwright / process globals never cross).
"""
import json
import hashlib
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .db import DATA_DIR, WORKSPACES_DIR

STUDIO_DIR = Path(__file__).resolve().parent.parent          # .../studio
DISTILL_DIR = Path(os.environ.get(
    "PPTAGENT_INFERENCE_ROOT",
    STUDIO_DIR.parent / ("inference" if (STUDIO_DIR.parent / "inference").is_dir() else "inference"),
)).expanduser().resolve()
PROJECT_ROOT = DISTILL_DIR.parent
SERVE_ONE = DISTILL_DIR / "serve_one.py"
JOBS_DIR = DATA_DIR / "jobs"
SENSE_PRESENT_V2_ROOT = DISTILL_DIR.parent / "vendor" / "sense-present-v2"
SENSE_PRESENT_V2_SKILLS_ROOT = SENSE_PRESENT_V2_ROOT / "skills"
LOCAL_PIPELINE_ROOT = DISTILL_DIR.parent / "vendor" / "static_ppt-clean-current"
CLEAN_PIPELINE_ROOT = Path(
    os.environ.get(
        "PPTAGENT_CLEAN_PIPELINE_ROOT",
        LOCAL_PIPELINE_ROOT,
    )
)
CLEAN_SKILLS_ROOT = CLEAN_PIPELINE_ROOT / "skills"
LONG_HORIZON_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_SKILLS_ROOT",
        "/mnt/afs/hejiatong/multimodal_design/ppt-html-pipeline/skills",
    )
)
VISUAL_CRAFT_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_SKILL_ROOT",
        "/mnt/afs/hejiatong/multimodal_design/"
        "ppt-html-pipeline-v3-speech-md/skills/ppt-skill-html",
    )
)
VISUAL_CRAFT_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_HARNESS_ROOT",
        "/mnt/afs/hejiatong/multimodal_design/ppt-html-pipeline-v3-speech-md",
    )
)
VISUAL_CRAFT_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_VISUAL_CRAFT_HARNESS_ENTRY", "distill_ppt.py"
)
VISUAL_CRAFT_MOUNT_ROOT = DATA_DIR / "skill-mounts" / "visual-craft"
_BUNDLED_LONG_HORIZON_PRESENTER_SUITE_ROOT = (
    PROJECT_ROOT / "bundled" / "static-ppt-skill-suite"
)
_DEFAULT_LONG_HORIZON_PRESENTER_SUITE_ROOT = (
    _BUNDLED_LONG_HORIZON_PRESENTER_SUITE_ROOT
    if (
        _BUNDLED_LONG_HORIZON_PRESENTER_SUITE_ROOT / "skills" / "long-horizon-presenter"
    ).is_dir()
    else Path("/mnt/afs/hejiatong/multimodal_design/static-ppt-skill-suite")
)
LONG_HORIZON_PRESENTER_SUITE_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_PRESENTER_SUITE_ROOT",
        _DEFAULT_LONG_HORIZON_PRESENTER_SUITE_ROOT,
    )
)
LONG_HORIZON_PRESENTER_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_PRESENTER_SKILL_ROOT",
        LONG_HORIZON_PRESENTER_SUITE_ROOT / "skills" / "long-horizon-presenter",
    )
)
LONG_HORIZON_PRESENTER_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_PRESENTER_HARNESS_ROOT",
        LONG_HORIZON_PRESENTER_SUITE_ROOT / "harnesses" / "long-horizon-presenter",
    )
)
LONG_HORIZON_PRESENTER_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_LONG_HORIZON_PRESENTER_HARNESS_ENTRY", "distill_ppt.py"
)
MURAL_PRESENTER_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_PRESENTER_SKILL_ROOT",
        LONG_HORIZON_PRESENTER_SUITE_ROOT / "skills" / "mural-presenter",
    )
)
MURAL_PRESENTER_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_PRESENTER_HARNESS_ROOT",
        LONG_HORIZON_PRESENTER_SUITE_ROOT / "harnesses" / "mural-presenter",
    )
)
MURAL_PRESENTER_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_PRESENTER_HARNESS_ENTRY", "distill_ppt.py"
)
ENGINE_SITE_PACKAGES = Path(
    os.environ.get(
        "PPTAGENT_ENGINE_SITE_PACKAGES",
        DATA_DIR / "engine-runtime" / "site-packages",
    )
)
CHROMIUM_DEPS_DIR = Path(os.environ.get("PPTAGENT_CHROMIUM_DEPS", DATA_DIR / "chromium-deps"))
# vLLM serve 把当前端点写进哨兵文件(serve 重起→IP 变→文件自动更新)。studio 派发时优先读它,
# 读不到/不合法再退回 MODELS 里硬编码的 base_url。根目录可用 PPTAGENT_SERVE_SENTINEL_DIR 覆盖。
SERVE_SENTINEL_DIR = Path(
    os.environ.get("PPTAGENT_SERVE_SENTINEL_DIR", DATA_DIR / "serve-sentinels")
)


def _hash_runtime_tree(digest: "hashlib._Hash", root: Path, *, prefix: str = "") -> None:
    """Hash runtime sources while pruning virtualenvs and test/cache trees."""
    ignored_dirs = {".git", ".pytest_cache", ".venv", "__pycache__", "tests"}
    files: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True):
        directories[:] = sorted(name for name in directories if name not in ignored_dirs)
        base = Path(current)
        files.extend(
            base / name
            for name in sorted(names)
            if not name.endswith(".pyc")
        )
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(prefix.encode("utf-8"))
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())

def _clean_skill(language: str, label: str) -> dict:
    name = f"ppt-skill-html-clean-{language}"
    return {
        "label": label,
        "path": str(CLEAN_SKILLS_ROOT / name),
        "name": name,
        "language": language,
        "mode": "clean-bilingual",
        "status": "current",
        "ready": True,
        "pipeline": "infer",
        "harness_path": str(CLEAN_PIPELINE_ROOT),
        "harness_entry": "infer.py",
        "pairing": "clean-current",
        "caps": ["attachments", "revision"],
    }


# Auto 是 Studio 的建任务策略：main.py 会在入队前按 query/JSONL lang 解析成 zh/en。
# 这里保留未绑定语言目录的定义，仅用于兼容历史数据库中的 auto 任务。
def _auto_skill() -> dict:
    return {
        "label": "Auto（自动选择）",
        "path": str(CLEAN_SKILLS_ROOT),
        "name": None,
        "language": "auto",
        "mode": "clean-bilingual",
        "status": "current",
        "ready": True,
        "pipeline": "infer",
        "harness_path": str(CLEAN_PIPELINE_ROOT),
        "harness_entry": "infer.py",
        "pairing": "clean-current",
        "caps": ["attachments"],
    }


def _long_horizon_skill(
    name: str = "long-horizon-html-ppt",
    label: str = "Long-horizon HTML PPT",
    pairing: str = "long-horizon-current",
    skills_root: Path = LONG_HORIZON_SKILLS_ROOT,
    inline_image: bool = False,
) -> dict:
    path = skills_root / name
    # These files form the minimum runnable contract.  The revision below is
    # intentionally calculated from the *entire* source tree so newly added
    # role/reference/script files are picked up without another Studio change.
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base.css",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/image_background.py",
        "scripts/render_deck.py",
        "scripts/workspace_policy.py",
        "roles/material.md",
        "roles/research.md",
        "roles/slide.md",
        "roles/review.md",
        "references/materials-and-images.md",
        "references/html-contract.md",
        "references/fonts-and-type.md",
        "references/charts-and-diagrams.md",
        "references/plan-contract.md",
        "references/page-patterns.md",
        "references/visual-direction.md",
    ) + (
        # The current Grouped Skill deliberately keeps the root Orchestrator
        # contract in SKILL.md (its "唯一根工作流") instead of duplicating it
        # in a role card. Other Long-horizon variants still use the separate
        # role file and must continue to validate it.
        () if name == "long-horizon-html-ppt-grouped"
        else ("roles/orchestrator.md",)
    ) + (
        ("references/slide-image-routing.md",)
        if inline_image
        else ("roles/image.md",)
    )
    missing_files = [relative for relative in required_files if not (path / relative).is_file()]
    source_files = sorted(
        file.relative_to(path).as_posix()
        for file in path.rglob("*")
        if file.is_file()
        and file.suffix != ".pyc"
        and "__pycache__" not in file.parts
        and not any(part.startswith(".") for part in file.relative_to(path).parts)
    ) if path.is_dir() else []
    digest = hashlib.sha256()
    if not missing_files:
        for relative in source_files:
            digest.update(relative.encode("utf-8"))
            digest.update((path / relative).read_bytes())
    ready = not missing_files
    return {
        "label": label,
        "path": str(path),
        "skills_root": str(skills_root),
        "name": name,
        # Skill instructions are Chinese, while the delivered deck follows the query language.
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "long-horizon",
        "status": "current",
        "ready": ready,
        "unavailable_reason": (
            f"{name} 缺少：" + ", ".join(missing_files)
            if missing_files else ""
        ),
        "required_files": list(required_files),
        "source_files": source_files,
        "source_file_count": len(source_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "infer",
        "harness_path": str(CLEAN_PIPELINE_ROOT),
        "harness_entry": "infer.py",
        "pairing": pairing,
        "caps": ["attachments"],
    }


def _visual_craft_mount(skill_root: Path = VISUAL_CRAFT_SKILL_ROOT) -> tuple[Path, str]:
    """Expose the direct snapshot as ``skills/ppt-skill-html`` for its Harness.

    The legacy catalog stores one Skill at a path named ``skill`` while the
    paired Harness deliberately resolves ``PPT_SKILLS_ROOT/ppt-skill-html``.
    A tiny Studio-owned symlink preserves that contract without copying or
    modifying the externally owned snapshot.
    """
    mount_root = VISUAL_CRAFT_MOUNT_ROOT
    link = mount_root / "ppt-skill-html"
    try:
        mount_root.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(link):
            if link.is_symlink() and link.resolve(strict=False) == skill_root.resolve(strict=False):
                return mount_root, ""
            if link.is_symlink():
                # This is a Studio-owned compatibility mount. The configured
                # canonical snapshot may change, so update only this symlink;
                # never replace a real directory or file at the mount point.
                link.unlink()
                link.symlink_to(skill_root, target_is_directory=True)
                return mount_root, ""
            return mount_root, f"Skill 挂载点已被其他文件占用：{link}"
        link.symlink_to(skill_root, target_is_directory=True)
        return mount_root, ""
    except OSError as exc:
        return mount_root, f"无法创建 Skill 挂载点：{exc}"


def _visual_craft_pipeline_contract(
    harness_root: Path = VISUAL_CRAFT_HARNESS_ROOT,
    entry: str = VISUAL_CRAFT_HARNESS_ENTRY,
) -> tuple[bool, str, list[str]]:
    required = (
        entry,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    if not os.access(harness_root, os.R_OK | os.X_OK):
        return False, f"Visual Craft Harness 目录无读取/遍历权限：{harness_root}", list(required)
    missing = [relative for relative in required if not (harness_root / relative).is_file()]
    if missing:
        return False, "Visual Craft Harness 缺少或无权读取：" + ", ".join(missing), list(required)
    return True, "", list(required)


def _visual_craft_skill(
    skill_root: Path = VISUAL_CRAFT_SKILL_ROOT,
    harness_root: Path = VISUAL_CRAFT_HARNESS_ROOT,
) -> dict:
    """Register the legacy visual-first Skill under a product-facing name."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/quality-checklist.md",
        "scripts/render.py",
        "scripts/font_bundle.py",
        "scripts/build_player.py",
        "scripts/stage_materials.py",
        "subagents/research.md",
        "subagents/material.md",
        "subagents/image.md",
        "subagents/slide.md",
        "subagents/review.md",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    harness_ready, harness_reason, harness_required = _visual_craft_pipeline_contract(harness_root)
    mount_root, mount_reason = _visual_craft_mount(skill_root)
    reasons = []
    if missing_skill:
        reasons.append("Visual Craft Skill 缺少或无权读取：" + ", ".join(missing_skill))
    if harness_reason:
        reasons.append(harness_reason)
    if mount_reason:
        reasons.append(mount_reason)

    snapshot = {}
    try:
        snapshot = json.loads((skill_root / ".snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    digest = hashlib.sha256()
    if not missing_skill:
        for path in sorted(
            (item for item in skill_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(skill_root).as_posix(),
        ):
            relative = path.relative_to(skill_root).as_posix()
            if any(
                part in {".venv", "__pycache__", ".pytest_cache"}
                for part in path.parts
            ) or path.suffix == ".pyc":
                continue
            digest.update(relative.encode("utf-8"))
            digest.update(path.read_bytes())
    ready = not missing_skill and harness_ready and not mount_reason
    return {
        "label": "Visual Craft HTML PPT",
        "path": str(skill_root),
        "skills_root": str(mount_root),
        "name": "ppt-skill-html",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "visual-craft",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": (
            str(snapshot.get("tree_hash") or "")[:12]
            or digest.hexdigest()[:12]
        ),
        "snapshot_id": str(snapshot.get("id") or ""),
        "pipeline": "visual-craft-harness",
        "harness_path": str(harness_root),
        "harness_entry": VISUAL_CRAFT_HARNESS_ENTRY,
        "harness_required_files": harness_required,
        "pairing": "visual-craft-paired",
        "caps": ["attachments", "revision"],
    }


def _long_horizon_presenter_skill(
    skill_root: Path = LONG_HORIZON_PRESENTER_SKILL_ROOT,
    harness_root: Path = LONG_HORIZON_PRESENTER_HARNESS_ROOT,
) -> dict:
    """Register the renamed static high-design Skill with its paired Harness."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/licenses/OFL-1.1.txt",
        "assets/vendor/echarts.min.js",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/editing-contract.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/planning-contract.md",
        "references/quality-checklist.md",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/install.sh",
        "scripts/image_cutout.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "subagents/research.md",
        "subagents/material.md",
        "subagents/image.md",
        "subagents/slide.md",
        "subagents/review.md",
    )
    harness_required = (
        LONG_HORIZON_PRESENTER_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    for frozen_name in ("sn-ppt-web-zh", "sn-ppt-web-en"):
        if not (skill_root.parent / frozen_name / "SKILL.md").is_file():
            missing_skill.append(f"../{frozen_name}/SKILL.md")
    missing_harness = [relative for relative in harness_required if not (harness_root / relative).is_file()]
    reasons = []
    if missing_skill:
        reasons.append("long-horizon-presenter Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("long-horizon-presenter Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root)
        _hash_runtime_tree(digest, skill_root.parent / "sn-ppt-web-zh", prefix="sn-ppt-web-zh/")
        _hash_runtime_tree(digest, skill_root.parent / "sn-ppt-web-en", prefix="sn-ppt-web-en/")
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": os.environ.get(
            "PPTAGENT_LONG_HORIZON_PRESENTER_DISPLAY_NAME",
            "sn-ppt-web",
        ),
        "path": str(skill_root),
        "skills_root": str(skill_root.parent),
        "name": "long-horizon-presenter",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "long-horizon-presenter",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "long-horizon-presenter-harness",
        "harness_path": str(harness_root),
        "harness_entry": LONG_HORIZON_PRESENTER_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "long-horizon-presenter-paired",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
    }


def _mural_presenter_skill(
    skill_root: Path = MURAL_PRESENTER_SKILL_ROOT,
    harness_root: Path = MURAL_PRESENTER_HARNESS_ROOT,
) -> dict:
    """Register Mural Presenter with the Harness shipped in the same suite."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base-template.css",
        "references/charts-and-diagrams.md",
        "references/design-rules.md",
        "references/editing-guide.md",
        "references/fonts.md",
        "references/html-guide.md",
        "references/layout-patterns.md",
        "references/planning-guide.md",
        "references/quality-checklist.md",
        "references/scenario-routing.md",
        "references/shape-grammar.md",
        "references/style-routing.md",
        "scripts/bundle_fonts.py",
        "scripts/cutout_image.py",
        "scripts/deck.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "roles/research.md",
        "roles/material.md",
        "roles/image.md",
        "roles/slide.md",
        "roles/review.md",
    )
    harness_required = (
        MURAL_PRESENTER_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    missing_harness = [relative for relative in harness_required if not (harness_root / relative).is_file()]
    reasons = []
    if missing_skill:
        reasons.append("mural-presenter Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("mural-presenter Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root)
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": "sn-ppt-web",
        "path": str(skill_root),
        "skills_root": str(skill_root.parent),
        "name": "mural-presenter",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "mural-presenter",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "mural-presenter-harness",
        "harness_path": str(harness_root),
        "harness_entry": MURAL_PRESENTER_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "mural-presenter-paired",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
    }


def _sense_present_skill(name: str, label: str, pipeline: str, entry: str) -> dict:
    if name == "sn-ppt-standard":
        specific = (
            "sn-ppt-standard/SKILL.md",
            "sn-ppt-standard/scripts/render.py",
            "sn-ppt-standard/scripts/build_player.py",
        )
        output = "static_html"
    elif name == "sn-ppt-dazzle":
        specific = (
            "sn-ppt-dazzle/SKILL.md",
            "sn-ppt-dazzle/scripts/init_deck.py",
            "sn-ppt-dazzle/scripts/render_deck.py",
            "sn-ppt-dazzle/scripts/harvest_deck.py",
        )
        output = "dynamic_html"
    else:
        raise ValueError(f"unknown direct Sense Present Skill: {name}")
    required = (
        "sn-ppt-story/SKILL.md",
        "sn-ppt-tools/references/capability-policy.md",
        *specific,
    )
    missing = [name for name in required if not (SENSE_PRESENT_V2_SKILLS_ROOT / name).is_file()]
    upstream = {}
    try:
        upstream = json.loads((SENSE_PRESENT_V2_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    ready = not missing and (DISTILL_DIR / "sense_present_v2.py").is_file() \
        and (DISTILL_DIR / entry).is_file()
    return {
        "label": label,
        "path": str(SENSE_PRESENT_V2_SKILLS_ROOT / name),
        "skills_root": str(SENSE_PRESENT_V2_SKILLS_ROOT),
        "name": name,
        "language": "zh",
        "deck_language": "auto",
        "mode": name,
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "缺少：" + ", ".join(missing) if missing else "",
        "source_revision": str(upstream.get("commit") or "8481ba94")[:12],
        "pipeline": pipeline,
        "pairing": f"{name}-alpha.1",
        "output": output,
        "caps": ["attachments", "revision", output],
        "runtime_env": {"SENSENOVA_PPT_V2_ROOT": str(SENSE_PRESENT_V2_ROOT)},
    }


# 顺序同时决定 UI 顺序。普通 Long-horizon 从 Pipeline 热加载；Grouped 版本在
# Web Demo vendor 中维护独立副本。两者都在任务启动时快照进对应 deck 工作区。
SKILLS = {
    "sense-present-standard": _sense_present_skill(
        "sn-ppt-standard", "SenseNova Static HTML", "sense-present-standard-harness",
        "sense_present_standard.py",
    ),
    "sense-present-dazzle": _sense_present_skill(
        "sn-ppt-dazzle", "SenseNova Dynamic HTML", "sense-present-dazzle-harness",
        "sense_present_dazzle.py",
    ),
    "auto": _auto_skill(),
    "zh": _clean_skill("zh", "中文 Skill"),
    "en": _clean_skill("en", "English Skill"),
    "long-horizon": _long_horizon_skill(),
    "long-horizon-grouped": _long_horizon_skill(
        name="long-horizon-html-ppt-grouped",
        label="Long-horizon HTML PPT Grouped",
        pairing="long-horizon-grouped-current",
        skills_root=CLEAN_SKILLS_ROOT,
    ),
    "long-horizon-grouped-inline-image": _long_horizon_skill(
        name="long-horizon-html-ppt-grouped-inline-image",
        label="Long-horizon Grouped · Inline Image",
        pairing="long-horizon-grouped-inline-image-ab",
        skills_root=CLEAN_SKILLS_ROOT,
        inline_image=True,
    ),
    "visual-craft": _visual_craft_skill(),
    "long-horizon-presenter": _long_horizon_presenter_skill(),
    "mural-presenter": _mural_presenter_skill(),
}
# Product surface: three static generation modes, each backed by its own paired
# Harness.  Dynamic generation is selected in its own UI mode and is paired with
# sense-present-dazzle-harness rather than being mixed into this static catalog.
_DEFAULT_PUBLIC_SKILL_KEYS = (
    "sense-present-standard",
    "visual-craft",
    "long-horizon-presenter",
    "mural-presenter",
)
_configured_public_skill_keys = tuple(
    key.strip()
    for key in os.environ.get("PPTAGENT_PUBLIC_SKILL_KEYS", "").split(",")
    if key.strip() in SKILLS
)
PUBLIC_SKILL_KEYS = _configured_public_skill_keys or _DEFAULT_PUBLIC_SKILL_KEYS
_configured_default_skill = os.environ.get("PPTAGENT_DEFAULT_SKILL", "").strip()
DEFAULT_SKILL = (
    _configured_default_skill
    if _configured_default_skill in PUBLIC_SKILL_KEYS
    else ("mural-presenter" if "mural-presenter" in PUBLIC_SKILL_KEYS else PUBLIC_SKILL_KEYS[0])
)
_SKILL_CATALOG_LOCK = threading.Lock()
try:
    _SKILL_CATALOG_REFRESH_TTL_S = max(
        0.0, float(os.environ.get("PPTAGENT_SKILL_REFRESH_TTL_S", "10"))
    )
except ValueError:
    _SKILL_CATALOG_REFRESH_TTL_S = 10.0
# SKILLS was built from the current source trees immediately above. Treat that
# import-time snapshot as fresh so the first homepage request does not hash all
# Skill files on AFS again.
_SKILL_CATALOG_REFRESHED_AT = time.monotonic()


def refresh_external_skills(*, force: bool = False) -> dict:
    """Refresh externally owned Skills without restarting Studio.

    Replacing catalog entries atomically keeps page rendering and job creation
    on one coherent snapshot of each Skill's readiness and content revision.
    Homepage rendering uses a short cache because hashing every Skill tree on
    AFS dominates its latency; job construction forces a refresh below.
    """
    global _SKILL_CATALOG_REFRESHED_AT
    with _SKILL_CATALOG_LOCK:
        now = time.monotonic()
        if (
            not force
            and now - _SKILL_CATALOG_REFRESHED_AT < _SKILL_CATALOG_REFRESH_TTL_S
        ):
            return SKILLS["long-horizon"]
        refreshed = _long_horizon_skill()
        grouped = _long_horizon_skill(
            name="long-horizon-html-ppt-grouped",
            label="Long-horizon HTML PPT Grouped",
            pairing="long-horizon-grouped-current",
            skills_root=CLEAN_SKILLS_ROOT,
        )
        grouped_inline_image = _long_horizon_skill(
            name="long-horizon-html-ppt-grouped-inline-image",
            label="Long-horizon Grouped · Inline Image",
            pairing="long-horizon-grouped-inline-image-ab",
            skills_root=CLEAN_SKILLS_ROOT,
            inline_image=True,
        )
        visual_craft = _visual_craft_skill()
        long_horizon_presenter = _long_horizon_presenter_skill()
        mural_presenter = _mural_presenter_skill()
        SKILLS["long-horizon"] = refreshed
        SKILLS["long-horizon-grouped"] = grouped
        SKILLS["long-horizon-grouped-inline-image"] = grouped_inline_image
        SKILLS["visual-craft"] = visual_craft
        SKILLS["long-horizon-presenter"] = long_horizon_presenter
        SKILLS["mural-presenter"] = mural_presenter
        if "PIPELINES" in globals():
            PIPELINES["visual-craft-harness"] = _visual_craft_pipeline(visual_craft)
            PIPELINES["long-horizon-presenter-harness"] = _long_horizon_presenter_pipeline(
                long_horizon_presenter
            )
            PIPELINES["mural-presenter-harness"] = _mural_presenter_pipeline(
                mural_presenter
            )
        _SKILL_CATALOG_REFRESHED_AT = time.monotonic()
    return refreshed


# zh/en 共用同一套 clean Harness；语言选择只决定 Skill，不复制 Pipeline。
def _visual_craft_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("visual-craft") or _visual_craft_skill()
    return {
        "label": "Visual Craft Harness",
        "path": str(VISUAL_CRAFT_HARNESS_ROOT),
        "entry": VISUAL_CRAFT_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "ppt-skill-html",
        "caps": ["attachments", "revision"],
        "pairing": "visual-craft-paired",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _long_horizon_presenter_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("long-horizon-presenter") or _long_horizon_presenter_skill()
    return {
        "label": f"{skill['label']} harness",
        "path": str(LONG_HORIZON_PRESENTER_HARNESS_ROOT),
        "entry": LONG_HORIZON_PRESENTER_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "long-horizon-presenter",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "long-horizon-presenter-paired",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_presenter_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter") or _mural_presenter_skill()
    return {
        "label": "sn-ppt-web harness",
        "path": str(MURAL_PRESENTER_HARNESS_ROOT),
        "entry": MURAL_PRESENTER_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-paired",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


PIPELINES = {
    "sense-present-standard-harness": {
        "label": "SenseNova Static HTML Harness",
        "path": str(DISTILL_DIR),
        "entry": "sense_present_standard.py",
        "supports": ["anthropic", "openai"],
        "skill_mode": "sn-ppt-standard",
        "caps": ["attachments", "revision", "static_html"],
        "pairing": "sn-ppt-standard-alpha.1",
        "ready": bool(SKILLS["sense-present-standard"].get("ready")),
        "unavailable_reason": SKILLS["sense-present-standard"].get("unavailable_reason", ""),
    },
    "sense-present-dazzle-harness": {
        "label": "SenseNova Dynamic HTML Harness",
        "path": str(DISTILL_DIR),
        "entry": "sense_present_dazzle.py",
        "supports": ["anthropic", "openai"],
        "skill_mode": "sn-ppt-dazzle",
        "caps": ["attachments", "revision", "dynamic_html"],
        "pairing": "sn-ppt-dazzle-alpha.1",
        "ready": bool(SKILLS["sense-present-dazzle"].get("ready")),
        "unavailable_reason": SKILLS["sense-present-dazzle"].get("unavailable_reason", ""),
    },
    "infer": {
        "label": "Clean infer harness",
        "path": str(CLEAN_PIPELINE_ROOT),
        "entry": "infer.py",
        "supports": ["anthropic", "openai"],
        "skill_mode": "clean-bilingual",
        "caps": ["attachments", "revision"],
        "pairing": "clean-current",
        "ready": True,
    },
    "visual-craft-harness": _visual_craft_pipeline(),
    "long-horizon-presenter-harness": _long_horizon_presenter_pipeline(),
    "mural-presenter-harness": _mural_presenter_pipeline(),
}
DEFAULT_PIPELINE = SKILLS[DEFAULT_SKILL].get("pipeline", "mural-presenter-harness")

# 历史 deck 仍可重试，但旧 key 不再注册、也不会出现在前端选项中。
LEGACY_SKILL_ALIASES = {
    "sense-present-v2": "sense-present-standard",
    "current": "zh",
    "v5": "zh",
    "v4": "zh",
    "v3": "zh",
    "v2": "zh",
    "v1": "zh",
}

# 可选模型注册表:anthropic 走引擎默认 teacher 路径;openai 走引擎的 student shim
# (agent_loop 按 MODEL_BACKEND=openai + STUDENT_BASE_URL/STUDENT_MODEL 切换)。
# Public V1 intentionally ships without built-in model endpoints.
# Configure SENSENOVA_MODEL_* or add models from User -> Model configuration.
MODELS = {}
DEFAULT_MODEL = "deployment-model"
ENVIRONMENT_MODEL_KEY = "deployment-model"


def _environment_model() -> dict | None:
    """Build the optional deployment-managed model as its own registry item.

    ``SENSENOVA_MODEL_*`` used to override the first built-in model in
    ``selection_env``.  That made the label, model id and endpoint come from
    different sources.  Treating the environment configuration as a distinct
    model keeps the catalog truthful and lets deployments start with no model
    at all.
    """
    base_url = os.environ.get("SENSENOVA_MODEL_BASE_URL", "").strip().rstrip("/")
    model_name = os.environ.get("SENSENOVA_MODEL_NAME", "").strip()
    if not base_url or not model_name:
        return None
    return {
        "label": (
            os.environ.get("SENSENOVA_MODEL_DISPLAY_NAME", "").strip()
            or model_name
        ),
        "backend": "openai",
        "engine_model": model_name,
        "base_url": base_url,
        "api_key": os.environ.get("SENSENOVA_MODEL_API_KEY", "").strip(),
        "slide_concurrency": int(
            os.environ.get("SENSENOVA_MODEL_SLIDE_CONCURRENCY", "4") or "4"
        ),
        "thinking_mode": "toggle",
        "thinking_transport": (
            os.environ.get("SENSENOVA_MODEL_THINKING_TRANSPORT", "openai")
            .strip().lower()
        ),
        "deployment_managed": True,
    }


_ENVIRONMENT_MODEL = _environment_model()
if _ENVIRONMENT_MODEL:
    MODELS[ENVIRONMENT_MODEL_KEY] = _ENVIRONMENT_MODEL

# 老 deck 的 DB 里可能存了历史 model key(label/id 漂移期或旧版本留下的),映射到现行注册表 key,
# 保证老 deck 仍可解析、不报「未知模型」。新建 deck 一律存归一后的现行 key。
ALIASES = {
    "opus-4.8": "opus-4.7-thinking",
    "opus-4.8-thinking": "opus-4.7-thinking",
    "opus-4.7": "opus-4.7-thinking",
    "opus-4.6": "opus-4.7-thinking",
}


def canon(model_key: str):
    """把(可能是旧的)model key 归一到现行注册表 key。空→默认;无法识别→None。"""
    if not model_key:
        return DEFAULT_MODEL
    if model_key in MODELS:
        return model_key
    alias = ALIASES.get(model_key)
    if alias in MODELS:
        return alias
    return None


def model_option_label(model: dict) -> str:
    """下拉框展示注册表里的友好别名。"""
    return (model.get("label") or model.get("engine_model") or "").strip()


def thinking_capability(model_key: str = "", model_config: dict | None = None) -> dict:
    """Return the model's user-controllable thinking contract."""
    model = model_config
    if model is None:
        key = canon(model_key)
        model = MODELS.get(key or "", {})
    mode = str(model.get("thinking_mode") or "none")
    transport = str(model.get("thinking_transport") or "")
    if mode not in {"toggle", "always", "none"}:
        mode = "none"
    return {
        "mode": mode,
        "toggle": mode == "toggle" and bool(transport),
        "transport": transport if mode == "toggle" else "",
    }


def resolve_thinking(model_key: str, requested: bool,
                     model_config: dict | None = None) -> dict:
    """Resolve the UI choice into the behavior the backend will actually use."""
    capability = thinking_capability(model_key, model_config=model_config)
    if capability["mode"] == "always":
        effective = True
    elif capability["toggle"]:
        effective = bool(requested)
    else:
        effective = False
    return {
        **capability,
        "requested": bool(requested),
        "effective": effective,
    }


def user_selectable_models():
    """Return deployment-visible built-ins plus the optional env model.

    When ``PPTAGENT_PUBLIC_MODEL_KEYS`` is absent, development keeps the
    historical four-model catalog.  Setting it to an empty value intentionally
    exposes no built-ins, which is the release default.  A complete
    ``SENSENOVA_MODEL_BASE_URL`` + ``SENSENOVA_MODEL_NAME`` pair is always
    registered as one additional, honestly labelled model.
    """
    development_defaults = (
        "sensenova-flash-lite-v39",
        "sensenova-flash-lite-v39-2",
        "sensenova-flash-lite-v39-4",
        "pptagent-qwen35-27b-ckpt1764",
    )
    configured = os.environ.get("PPTAGENT_PUBLIC_MODEL_KEYS")
    keys = list(development_defaults) if configured is None else [
        key.strip() for key in configured.split(",") if key.strip()
    ]
    if _ENVIRONMENT_MODEL and ENVIRONMENT_MODEL_KEY not in keys:
        keys.append(ENVIRONMENT_MODEL_KEY)
    return [
        (key, MODELS[key])
        for key in dict.fromkeys(keys)
        if key in MODELS and not MODELS[key].get("ui_hidden")
    ]


def pipeline_for_skill(skill_key: str):
    """Skill 是选择真源；所有静态 Skill 都返回各自配套的 infer Harness。"""
    skill = SKILLS.get(canon_skill(skill_key))
    return skill.get("pipeline") if skill else None


def canon_pipeline(pipeline_key: str, model_key: str = None, skill_key: str = None):
    """有 Skill 时强制使用其配对 Harness;否则规范化显式/默认 pipeline。"""
    if skill_key:
        return pipeline_for_skill(skill_key)
    if not pipeline_key:
        return DEFAULT_PIPELINE
    if pipeline_key in LEGACY_SKILL_ALIASES:
        return DEFAULT_PIPELINE
    return pipeline_key if pipeline_key in PIPELINES else None


def canon_skill(skill_key: str, pipeline_key: str = None):
    """空 skill 使用 Auto；历史版本静默迁移到 zh，未知返回 None。"""
    if not skill_key:
        return DEFAULT_SKILL
    if skill_key in LEGACY_SKILL_ALIASES:
        return LEGACY_SKILL_ALIASES[skill_key]
    return skill_key if skill_key in SKILLS else None


def default_generation_stack(model_key: str = None) -> dict:
    """UI-facing automatic model -> pipeline/skill mapping.

    The page no longer exposes pipeline selection. Keep this as the single
    adaptation point so future pipeline upgrades only change registry/defaults.
    """
    model_key = canon(model_key) or DEFAULT_MODEL
    skill_key = canon_skill("")
    pipeline_key = pipeline_for_skill(skill_key)
    return {
        "model": model_key,
        "pipeline": pipeline_key,
        "pipeline_label": PIPELINES[pipeline_key]["label"],
        "skill": skill_key,
        "skill_label": SKILLS[skill_key]["label"],
    }


def validate_selection(model_key: str, pipeline_key: str, skill_key: str,
                       model_config: dict | None = None):
    model = model_config or MODELS[canon(model_key) or DEFAULT_MODEL]
    skill = SKILLS.get(skill_key)
    if not skill:
        return "未知 skill"
    if not skill.get("ready", False):
        return skill.get("unavailable_reason") or "该 Skill 当前不可用"
    expected_pipeline = pipeline_for_skill(skill_key)
    if not expected_pipeline:
        return skill.get("unavailable_reason", "该 Skill 没有可运行的 Harness")
    if pipeline_key != expected_pipeline:
        return f"{skill['label']} 必须使用配对 Harness {expected_pipeline}"
    pipe = PIPELINES.get(expected_pipeline)
    if not pipe:
        return "未知 pipeline"
    if not pipe.get("ready", False):
        return pipe.get("unavailable_reason") or "该 Harness 当前不可用"
    if model["backend"] not in set(pipe["supports"]):
        return f"{pipe['label']} 暂不支持 {model['backend']} 模型"
    return None


# ---- C4: Opus(anthropic)key 自动失败转移 -------------------------------------
# 引擎子进程自读 .env(load_dotenv 用 setdefault,不覆盖已设的)→ studio 在 child_env 里
# 注入探活后的 ANTHROPIC_API_KEY 即可覆盖引擎默认 key。key 池 + base_url 从 DISTILL_DIR/.env
# 读(studio 自身 environ 的 ANTHROPIC_* 已被 jobs.py 剔除,故直接读文件):
#   ANTHROPIC_API_KEY            主 key
#   ANTHROPIC_API_KEY_FALLBACKS  备用 key(逗号分隔,主挂了按序探活切过去)
_ANTHROPIC_ENV_FILE = DISTILL_DIR / ".env"
_KEY_CACHE = {"key": None, "ts": 0.0}
_KEY_CACHE_TTL = 120.0            # 探活结果缓存;deck 派发不是高频路径,120s 足够且省探活开销
_REFRESH_LOCK = threading.Lock()
_REFRESHING = {"on": False}


def _read_env_file(path) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _deployment_secret(name: str, file_env: dict | None = None) -> str:
    """Resolve a model credential from safe deployment aliases or .env."""
    namespaced = str(os.environ.get(f"SENSENOVA_{name}") or "").strip()
    if namespaced:
        return namespaced
    return str((file_env or _read_env_file(_ANTHROPIC_ENV_FILE)).get(name) or "").strip()


def _anthropic_pool():
    """(base_url, [keys 主→备])。备用 = ANTHROPIC_API_KEY_FALLBACKS(逗号分隔),去重、保序。"""
    env = _read_env_file(_ANTHROPIC_ENV_FILE)
    base = (
        os.environ.get("SENSENOVA_ANTHROPIC_BASE_URL")
        or env.get("ANTHROPIC_BASE_URL")
        or "https://tokenhub.sensetime.com"
    ).rstrip("/")
    keys = []
    primary = _deployment_secret("ANTHROPIC_API_KEY", env)
    if primary:
        keys.append(primary)
    fallbacks = (
        os.environ.get("SENSENOVA_ANTHROPIC_API_KEY_FALLBACKS")
        or env.get("ANTHROPIC_API_KEY_FALLBACKS")
        or ""
    )
    for k in fallbacks.split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return base, keys


def _probe_key(base_url, key, model="claude-opus-4-7-thinking", timeout=4) -> bool:
    """极小的 /v1/messages 探活:200=通道+key 都活;503(无可用通道)/401(坏 key)/超时=不可用。"""
    body = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        base_url + "/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200          # 4xx/5xx(含 503 无通道)一律不可用
    except Exception:
        return False                  # 超时 / 连不上 = 不可用


def _refresh_key(model: str = "claude-opus-4-7-thinking"):
    """探活主→备,把首个 200 的 key 写进缓存。探活是真 /v1/messages 调用(秒级、有波动),故只在后台线程跑。"""
    base, keys = _anthropic_pool()
    if not keys:
        return
    chosen = keys[0]                  # 兜底:全挂也用主 key
    for k in keys:
        if _probe_key(base, k, model):
            chosen = k
            break
    _KEY_CACHE.update(key=chosen, ts=time.time())


def resolve_anthropic_key(model: str = "claude-opus-4-7-thinking") -> str:
    """返回可用 Opus key —— **非阻塞**:命中新鲜缓存直接回;过期/未探活则立刻返回已知值(或主 key)
    并后台线程刷新。故 deck 派发路径永不卡在探活上(探活可能秒级)。语义保证:最坏也只退回主 key,
    即『没有 C4 时的原行为』—— C4 只会更好、绝不更糟。"""
    now = time.time()
    cached = _KEY_CACHE["key"]
    if cached and now - _KEY_CACHE["ts"] < _KEY_CACHE_TTL:
        return cached
    if not _REFRESHING["on"]:                  # 过期/未探活:触发一次后台刷新(不重复触发)
        with _REFRESH_LOCK:
            if not _REFRESHING["on"]:
                _REFRESHING["on"] = True

                def _bg():
                    try:
                        _refresh_key(model)
                    finally:
                        _REFRESHING["on"] = False

                threading.Thread(target=_bg, daemon=True).start()
    if cached:
        return cached                         # 有旧值先用(哪怕刚过期),后台随即更新
    base, keys = _anthropic_pool()            # 从未探活:先给主 key(=原行为),后台线程随即填好缓存
    return keys[0] if keys else ""


def resolve_base_url(model: dict) -> str:
    """openai 端点优先取 serve 哨兵文件(serve 重起自动更新),读不到/不合法再退回硬编码 base_url。
    哨兵一行格式:`http://HOST:PORT/v1  (model=ENGINE_MODEL)`。带 model= 注释时校验一致,
    防止哨兵串到别的模型上。"""
    hard = model.get("base_url", "")
    ef = model.get("endpoint_file")
    if not ef:
        return hard
    try:
        line = open(ef, encoding="utf-8").read().strip()
    except OSError:
        return hard
    if not line:
        return hard
    url = line.split()[0].rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        return hard
    if "model=" in line:                      # 哨兵标注了模型 → 校验一致,不一致就别信它
        ann = line.split("model=", 1)[1].strip().rstrip(")").strip()
        if model.get("engine_model") and ann != model["engine_model"]:
            return hard
    return url


def selection_env(model_key: str, pipeline_key: str, skill_key: str,
                  model_config: dict | None = None) -> dict:
    """该模型/pipeline/skill 在引擎子进程里需要的额外环境变量。"""
    skill_key = canon_skill(skill_key, pipeline_key)
    pipeline_key = pipeline_for_skill(skill_key)
    m = model_config or MODELS[canon(model_key) or DEFAULT_MODEL]
    pipe = PIPELINES[pipeline_key]
    skill = SKILLS[skill_key]
    env = {}
    effective_model_name = m["engine_model"]
    if m["backend"] == "openai":
        model_base_url = resolve_base_url(m)
        model_name = m["engine_model"]
        effective_model_name = model_name
        env.update({"MODEL_BACKEND": "openai",
                    "STUDENT_BASE_URL": model_base_url.rstrip("/"),
                    "STUDENT_MODEL": model_name,
                    # prompt caching 是 anthropic 专属:引擎会给 system 挂 cache_control,
                    # openai shim 原样塞进 payload,严格 openai 端点(tokenhub gpt-5.5)直接 400
                    # (Unknown parameter 'cache_control')。vLLM 学生本就忽略它,openai 一律关。
                    "PROMPT_CACHE": "0"})
        thinking = thinking_capability(model_key, model_config=m)
        if thinking["toggle"]:
            env["STUDIO_THINKING_TRANSPORT"] = thinking["transport"]
        # 认证型 openai 端点(如 tokenhub gpt-5.5):从本地 .env 读 key → STUDENT_API_KEY。
        # vLLM 学生没有 api_key_env,保持默认 EMPTY,不会把真 key 发到它们那里。
        direct_key = m.get("api_key", "")
        if direct_key:
            env["STUDENT_API_KEY"] = direct_key
        key_env = m.get("api_key_env")
        if key_env and not direct_key:
            key = _deployment_secret(key_env)
            if key:
                env["STUDENT_API_KEY"] = key
        # 自托管学生可拉高引擎子代理并发(引擎默认 4;SLIDE_CONCURRENCY 每个子进程独立读取)
        sc = m.get("slide_concurrency")
        if sc:
            env["SLIDE_CONCURRENCY"] = str(sc)
    elif m["backend"] == "anthropic":
        key = resolve_anthropic_key(m["engine_model"])   # C4:主 key 挂了自动切备用
        if key:
            env["ANTHROPIC_API_KEY"] = key
    env.update({
        # Keep the generic runtime selector aligned with STUDENT_MODEL when a
        # deployment overrides the built-in default model from .env/shell.
        "MODEL": effective_model_name,
        "CLEAN_SKILLS_DIR": skill.get("skills_root", str(CLEAN_SKILLS_ROOT)),
        # 空值是 clean infer 的自动语言选择契约。
        "CLEAN_FORCE_SKILL_LANGUAGE": skill.get(
            "force_skill_language",
            "" if skill["language"] == "auto" else skill["language"],
        ),
        # Reuse the Web Demo's credential file; the clean source tree remains
        # code/Skill/Harness only and never receives copied secrets.
        "CLEAN_DOTENV_PATH": str(_ANTHROPIC_ENV_FILE),
    })
    env["PPT_CAPABILITY_PROFILE"] = "visual"
    env.update(skill.get("runtime_env", {}))
    env.update({
        "PIPELINE_VERSION": pipeline_key,
        "PPT_SKILL_VERSION": skill_key,
        # Catalog sources are frozen inputs; importing a Harness must not
        # create __pycache__ inside the repository-owned snapshot.
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def deck_run_dir(user_id, deck_id) -> Path:
    return WORKSPACES_DIR / str(user_id) / "decks" / str(deck_id)


UPLOADS_DIR = DATA_DIR / "uploads"        # 附件独立存这里:引擎建 run_dir 用 exist_ok=False,不能被 studio 抢先创建


def deck_uploads_dir(deck_id) -> Path:
    return UPLOADS_DIR / str(deck_id)


def skill_mount_dir(skill_key: str, mode: str) -> str:
    """Return the selected Harness-compatible Skill root."""
    skill = SKILLS[canon_skill(skill_key) or DEFAULT_SKILL]
    return skill.get("skills_root", str(CLEAN_SKILLS_ROOT))


def build_job(sample_id: str, seed: dict, run_dir, dry: bool = False, model_key: str = None,
              pipeline_key: str = None, skill_key: str = None,
              model_config: dict | None = None) -> dict:
    model_key = canon(model_key) or DEFAULT_MODEL if model_config is None else model_key
    skill_key = canon_skill(skill_key, pipeline_key)
    if skill_key is None:
        raise ValueError("未知 skill")
    if skill_key in {
        "long-horizon",
        "long-horizon-grouped",
        "visual-craft",
        "long-horizon-presenter",
        "mural-presenter",
    }:
        # A generation job must capture the latest Skill even when the homepage
        # catalog is still inside its latency-oriented cache window.
        refresh_external_skills(force=True)
    pipeline_key = pipeline_for_skill(skill_key)
    if pipeline_key is None:
        raise ValueError(SKILLS[skill_key].get("unavailable_reason", "Skill 没有配对 Harness"))
    skill = SKILLS[skill_key]
    if not skill.get("ready", False):
        raise ValueError(skill.get("unavailable_reason") or "该 Skill 当前不可用")
    eng_seed = dict(seed)
    deck_language = skill.get("deck_language", skill["language"])
    if deck_language == "auto":
        eng_seed.pop("lang", None)
    else:
        eng_seed["lang"] = deck_language
    if eng_seed.get("slide_count") and not eng_seed.get("pages_hint"):
        eng_seed["pages_hint"] = int(eng_seed["slide_count"])
    pipe = PIPELINES[pipeline_key]
    job = {
        "sample_id": sample_id,
        "seed": eng_seed,
        "run_dir": str(run_dir),
        "dry_run": bool(dry),
        "batch": "studio",
        "pipeline_version": pipeline_key,
        "pipeline": pipe,
        "skill_version": skill_key,
        "skill": skill,
    }
    job["skill_mount_dir"] = skill_mount_dir(skill_key, pipe["skill_mode"])
    m = model_config or MODELS[model_key]
    job["model"] = m["engine_model"]      # serve_one 会把它覆盖进 config["model"]
    return job


def write_job_file(deck_id, job: dict) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    p = JOBS_DIR / f"{deck_id}.json"
    p.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def log_path(deck_id) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{deck_id}.log"


def _engine_python() -> str | None:
    env_python = os.environ.get("PPTAGENT_ENGINE_PYTHON") or os.environ.get("ENGINE_PYTHON")
    candidates = []
    if env_python:
        candidates.append(Path(env_python).expanduser())
    # Local deployments keep the heavy generation dependencies in the
    # inference project venv.  Prefer it over the host Python; the latter is
    # only a deployment fallback paired with ENGINE_SITE_PACKAGES.
    candidates.append(DISTILL_DIR / ".venv" / "bin" / "python")
    candidates.append(Path("/usr/bin/python3"))
    for python in candidates:
        if python.is_file() and os.access(python, os.X_OK):
            return str(python)
    return None


def _prepend_path(value: str, *prefixes: Path) -> str:
    parts = [str(p) for p in prefixes if p]
    if value:
        parts.append(value)
    return ":".join(parts)


def _playwright_revisions() -> dict[str, str]:
    """Read Chromium revisions required by the engine's Playwright package."""
    manifests = sorted(
        (DISTILL_DIR / ".venv" / "lib").glob(
            "python*/site-packages/playwright/driver/package/browsers.json"
        )
    )
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        revisions = {
            str(item.get("name")): str(item.get("revision"))
            for item in payload.get("browsers", [])
            if item.get("name") in {"chromium", "chromium-headless-shell"}
            and item.get("revision")
        }
        if revisions:
            return revisions
    return {}


def _installed_browser(cache: Path) -> Path | None:
    """Return an installed Playwright Chromium executable from *cache*.

    Playwright revisions change with the Python package, so the deployment must
    not pin a revision in Studio. Prefer the lightweight headless shell and
    retain full Chromium as a compatible fallback.
    """
    revisions = _playwright_revisions()
    patterns: list[str] = []
    headless_revision = revisions.get("chromium-headless-shell")
    chromium_revision = revisions.get("chromium")
    if headless_revision:
        patterns.append(
            f"chromium_headless_shell-{headless_revision}/"
            "chrome-headless-shell-linux64/chrome-headless-shell"
        )
    if chromium_revision:
        patterns.append(f"chromium-{chromium_revision}/chrome-linux64/chrome")
    # Custom/older Playwright distributions may omit browsers.json. Only in
    # that case is a generic installed Chromium preferable to no browser.
    if not patterns:
        patterns.extend((
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
            "chromium-*/chrome-linux64/chrome",
        ))
    for pattern in patterns:
        candidates = sorted(cache.glob(pattern), reverse=True)
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _browser_runtime(env: dict) -> tuple[Path, Path | None]:
    """Resolve a usable Playwright cache without relying on parent-shell env."""
    explicit_cache = env.get("PLAYWRIGHT_BROWSERS_PATH") or os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH"
    )
    explicit_exe = env.get("PPT_SKILL_BROWSER_EXE") or os.environ.get(
        "PPT_SKILL_BROWSER_EXE"
    )
    if explicit_exe:
        executable = Path(explicit_exe).expanduser()
        if executable.is_file() and os.access(executable, os.X_OK):
            cache = Path(explicit_cache).expanduser() if explicit_cache else executable.parents[2]
            return cache, executable

    cache_candidates: list[Path] = []
    if explicit_cache:
        cache_candidates.append(Path(explicit_cache).expanduser())
    cache_candidates.append(DATA_DIR / "ms-playwright")
    xdg_cache = env.get("XDG_CACHE_HOME") or os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        cache_candidates.append(Path(xdg_cache).expanduser() / "ms-playwright")
    cache_candidates.append(Path.home() / ".cache" / "ms-playwright")

    seen: set[str] = set()
    for cache in cache_candidates:
        cache = cache.resolve(strict=False)
        if str(cache) in seen:
            continue
        seen.add(str(cache))
        executable = _installed_browser(cache)
        if executable:
            return cache, executable

    # Preserve an explicit location for Playwright's own actionable error. If
    # none was supplied, keep the deployment-local bootstrap destination.
    fallback = Path(explicit_cache).expanduser() if explicit_cache else DATA_DIR / "ms-playwright"
    return fallback, None


def _browser_library_dirs(env: dict) -> list[Path]:
    """Find the deployed Chromium shared-library bundles."""
    configured = env.get("PPT_SKILL_BROWSER_LIB_DIRS") or os.environ.get(
        "PPT_SKILL_BROWSER_LIB_DIRS", ""
    )
    candidates = [Path(value).expanduser() for value in configured.split(os.pathsep) if value]
    lib_root = CHROMIUM_DEPS_DIR / "root" / "usr" / "lib"
    candidates.extend((
        lib_root / "x86_64-linux-gnu",
        lib_root,
        Path.home() / "pwdeps" / "lib",
        Path.home() / "cdeps" / "lib",
    ))
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        if not candidate.is_dir() or str(candidate) in seen:
            continue
        seen.add(str(candidate))
        result.append(candidate)
    return result


def render_env(env: dict | None = None) -> dict:
    """Add local Chromium shared libraries for Playwright rendering.

    Catalog skills start Playwright Chromium from slide-agent shell commands.
    CCI images do not always provide the NSS/NSPR/GTK libraries
    Chromium needs, so deploy_gateway.sh bootstraps them into studio/data and
    every engine job inherits that directory through LD_LIBRARY_PATH.
    """
    out = dict(env or {})
    browser_cache, browser_exe = _browser_runtime(out)
    browser_lib_dirs = _browser_library_dirs(out)
    out["PPTAGENT_CHROMIUM_DEPS"] = str(CHROMIUM_DEPS_DIR)
    out["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
    out["PPTAGENT_ENGINE_SITE_PACKAGES"] = str(ENGINE_SITE_PACKAGES)
    out["PYTHONPATH"] = _prepend_path(
        out.get("PYTHONPATH", ""),
        ENGINE_SITE_PACKAGES,
    )
    if browser_exe:
        out["PPT_SKILL_BROWSER_EXE"] = str(browser_exe)
    else:
        out.pop("PPT_SKILL_BROWSER_EXE", None)
    if browser_lib_dirs:
        out["PPT_SKILL_BROWSER_LIB_DIRS"] = os.pathsep.join(map(str, browser_lib_dirs))
    out["LD_LIBRARY_PATH"] = _prepend_path(
        out.get("LD_LIBRARY_PATH", ""),
        *browser_lib_dirs,
    )
    return out


def runner_cmd(job_path) -> list[str]:
    # Prefer an existing engine Python so deployments do not depend on `uv`
    # being present in PATH. Fall back to uv when no reusable venv exists.
    python = _engine_python()
    if python:
        return [python, str(SERVE_ONE), "--job", str(job_path)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(DISTILL_DIR),
                "python", str(SERVE_ONE), "--job", str(job_path)]
    raise RuntimeError(
        "No engine runtime found. Set PPTAGENT_ENGINE_PYTHON to a Python with "
        "inference dependencies, or install uv."
    )
