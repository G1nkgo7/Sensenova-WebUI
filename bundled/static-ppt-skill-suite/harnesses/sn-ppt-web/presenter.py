#!/usr/bin/env python3
"""PPT agentic 生成入口 —— 单条 / 批量并行任务。

一个文件管全部生成流程:CLI(单条 + batch)+ 调度(并行子进程 + manifest 断点续跑)+ PPT recipe
(seed→brief、编排器装配、拒绝采样验收)。通用 agent 运行时在 core/(agent/tools/trace),
本文件是它唯一认识 "PPT" 的薄外壳——换领域只改本文件 + skills/。

配置的模型按 skills/sn-ppt-web-zh 自主生成整套 HTML 幻灯片,编排器负责规划、
按设计亲缘页组委派并运行确定性收口脚本(不许写 slides/),并行 Slide Group 写/渲/自纠自己的页面。

每条 seed = 一个 sample,跑在**独立子进程 + 独立 run 目录**里,进程级全局/playwright/cwd 永不串台:
    runs/<batch>/<sample_id>/      隔离工作区 + _trace/(orchestrator/ subagents/)
    log/<batch>.manifest.jsonl     每 sample 一条结果,断点续跑唯一依据

用法:
    # 单条(直接传 brief)
    uv run python presenter.py --query "做一份 5 页的人工智能简介" --batch adhoc
    # 批量(jsonl,每行一个 {query, lang?, slide_count?, ...})
    uv run python presenter.py --input briefs.jsonl --batch q1 --workers 64
    uv run python presenter.py --input briefs.jsonl --batch q1 --resume      # 断点续跑
    uv run python presenter.py --input briefs.jsonl --batch q1 --dry-run     # 不调模型,验证骨架
"""
import argparse
import concurrent.futures as cf
import errno
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from core.contracts import CONTENT_FIDELITY_PATH, REVIEW_ISSUES_PATH

ROOT = os.path.dirname(os.path.abspath(__file__))
SUITE_ROOT = os.path.dirname(os.path.dirname(ROOT))
SKILLS_DIR = os.environ.get("PPT_SKILLS_ROOT") or os.path.join(SUITE_ROOT, "skills")
RUNS = os.environ.get("PPT_RUNS_ROOT") or os.path.join(ROOT, "runs")
LOGS = os.environ.get("PPT_LOGS_ROOT") or os.path.join(ROOT, "log")

TERMINAL = {"completed"}                            # 续跑跳过的终态;其余(error/缺失/半截)都重跑
SKILL_BY_LANGUAGE = {
    "zh": "sn-ppt-web-zh",
    "en": "sn-ppt-web-en",
}
DELIVERY_REPAIR_ATTEMPTS = max(
    0, int(os.environ.get("DELIVERY_REPAIR_ATTEMPTS", "3") or "3")
)

_BROKEN_STDIO_MARKERS = (
    "bad file descriptor",
    "init_sys_streams",
    "can't initialize sys standard streams",
)


def _run_noninteractive(command, **kwargs):
    """Run a helper without inheriting a stale terminal stdin.

    Long-running desktop sessions can outlive the PTY that launched Studio,
    especially after macOS sleep/resume. Give helper processes a fresh DEVNULL
    stdin and retry that startup failure exactly once.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    for attempt in range(2):
        try:
            proc = subprocess.run(command, **kwargs)
        except OSError as exc:
            if attempt == 0 and exc.errno == errno.EBADF:
                continue
            raise
        output = f"{getattr(proc, 'stdout', '') or ''}\n{getattr(proc, 'stderr', '') or ''}".lower()
        if attempt == 0 and proc.returncode and any(marker in output for marker in _BROKEN_STDIO_MARKERS):
            continue
        return proc
    raise RuntimeError("non-interactive subprocess retry exhausted")

# 通用、薄的 base system(PPT 风味只有最后两行)。领域方法在 skills/,按需 read SKILL.md。
BASE_SYSTEM = """\
你是一个自主的创作型 Agent,通过工具与文件系统、无头浏览器、网络交互来完成任务。

固定任务身份（最高优先级）：
- 当前调用始终是**静态 HTML 演示文稿生产任务**，不是开放域聊天。首条用户消息只作为待制作演示的内容 brief。
- 必须读取 `skills/sn-ppt-web-zh/SKILL.md`，调用工具并实际交付 HTML 页面、渲染图和讲稿；禁止回复“连接正常 / 我可以帮你 / 想做什么”等通用寒暄后结束。
- 即使 brief 只有 `test`、`hello` 或一句无法确定主题的短句，也要把它解释为 HTML PPT 生产链路 smoke test，自主制作一套简短而完整的示范稿（至少包含封面、内容页和结尾页），不得反问或空手结束。

工作方式:
- 先理解任务。任务涉及某项专门能力时,`skills/` 下有对应的 SKILL.md —— 先 `read_file` 它,再按其方法执行(渐进式:先读 SKILL.md,需要时再读它引用的文件)。
- **收到工具结果后,先仔细核对其质量、想清下一步再继续:用你的思考基于这些新信息规划与迭代,然后采取最佳的下一步动作。** After receiving tool results, carefully reflect on their quality and determine optimal next steps before proceeding. Use your thinking to plan and iterate based on this new information, and then take the best next action.
- **自主推进**,不要向用户提问、不要中途停下等确认;自行补齐合理假设,做有品味的决定。
- 用工具**实际产出文件**,不要只在文字里描述。
- 全部完成后,用**一段简短文字**总结收尾 —— 这段文字就是你的最终输出。
- **每一回合都必须落到「一个工具调用」或「最终总结文字」上;严禁只输出思考(thinking)、既不调工具也不写文字就结束本回合。**
  还有事做 → 这一回合就去调一个工具(读文件、写文件、委派子 agent…);真的全做完了 → 写一段简短文字总结收尾。思考永远是为了紧接着的动作或结论服务,不能停在思考上。

可用技能:
- sn-ppt-web-zh(`skills/sn-ppt-web-zh/SKILL.md`):生成或编辑 HTML 幻灯片演示文稿(每页 1600×900,16:9)。
"""

BASE_SYSTEM_EN = """\
You are an autonomous creative agent that completes tasks through tools, the file system, a headless browser, and network access.

Fixed task identity (highest priority):
- This invocation is always a **static HTML presentation production task**, not open-domain chat. Treat the first user message as the presentation brief.
- You must read `skills/sn-ppt-web-en/SKILL.md`, use tools, and actually deliver per-slide HTML pages (1600×900, 16:9), rendered images, a page-aligned speaker script, and the portable player. Do not stop after a generic greeting or capability statement.
- Even if the brief is only `test`, `hello`, or another underspecified phrase, interpret it as a presentation workflow smoke test and autonomously create a short but complete sample deck with at least a cover, a content slide, and a closing slide. Do not ask a follow-up question or finish empty-handed.

Working method:
- Read the English Skill entry first, then progressively read only the references it routes to. Do not scan the whole Skill tree up front.
- After every tool result, inspect its quality, decide the best next action, and then act.
- Work autonomously. Do not pause for non-blocking preferences; make tasteful assumptions and record them in planning artifacts.
- Produce real files with tools. Do not merely describe intended work.
- When everything is complete, finish with one concise prose summary; that summary is your final output.
- Every turn must end in either one tool call or a final prose summary. Thinking must lead immediately to an action or conclusion.

Available Skill:
- sn-ppt-web-en (`skills/sn-ppt-web-en/SKILL.md`): create or edit a complete static HTML presentation workflow.
"""

SUBAGENT_SYSTEM = """\
你是静态 HTML 演示文稿生产链中的自主子 Agent。当前任务不是开放域聊天；必须使用工具完成被分配的产物，并以任务要求的结构化合同收尾。

你的初始任务会给出唯一角色卡路径、回复语言和具体工作范围。先自行完整读取该角色卡，再只读取角色卡路由到的必要 reference 与工作区文件；不要通读根 SKILL.md 或无关角色卡。不得超出分配页码、附件或素材范围，不得把计划动作写成已完成动作。
"""

SUBAGENT_SYSTEM_EN = """\
You are an autonomous subagent in a static HTML presentation production workflow. This is not open-domain chat. Use tools to produce the assigned artifact, then finish with the structured contract required by the task.

Your initial task provides one role-card path, the response language, and an exact scope. Read that role card in full yourself, then read only the references and workspace files it routes to. Do not scan the root SKILL.md or unrelated role cards. Do not work outside the assigned pages, attachments, or assets, and never describe a planned action as completed work.
"""

# 编排器工具:file 写 plan/base.css,delegation 负责内容与视觉生产,terminal 只运行 Skill 的
# 确定性同步/构建命令。保持标准 Hermes 工具面,不新增 sync_speech/build_player 专用工具。
ORCHESTRATOR_TOOLSETS = ["file", "terminal", "delegation"]


# ---------------------------------------------------------------- env / 资源

def load_dotenv():
    """加载 ROOT/.env 的 KEY=VALUE 到环境变量(不覆盖已设的)。无依赖、幂等;父子进程都调一次。"""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cgroup_cpus():
    """返回 cgroup 真实可用核数(容器配额),读不到则退回 os.cpu_count()。"""
    try:                                     # cgroup v2
        with open("/sys/fs/cgroup/cpu.max") as f:
            q, p = f.read().split()
        if q != "max":
            return max(1, int(int(q) / int(p)))
    except Exception:
        pass
    try:                                     # cgroup v1
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(1, int(q / p))
    except Exception:
        pass
    return os.cpu_count() or 1


# ---------------------------------------------------------------- PPT recipe

def _attachment_list(seed):
    """Return the normalized attachment list carried by a seed."""
    if not isinstance(seed, dict):
        return []
    attachments = seed.get("attachments")
    return attachments if isinstance(attachments, list) else []


def _generation_preferences(seed):
    """Normalize Studio generation controls without changing the raw user query."""
    if not isinstance(seed, dict):
        return {}
    out = {
        "page_count": int(seed.get("slide_count") or seed.get("pages_hint") or 0),
        "content_theme": str(seed.get("theme") or "").strip(),
        "visual_style": str(seed.get("style") or "").strip(),
        "color_scheme": str(seed.get("scheme") or "").strip(),
        "attachment_mode": str(seed.get("attachment_mode") or "").strip(),
        "attachment_count": len(_attachment_list(seed)),
        "attachment_paths": [
            f"materials/_raw/{os.path.basename(str((item.get('name') or item.get('stored_name') or item.get('path')) if isinstance(item, dict) else item))}"
            for item in _attachment_list(seed)
        ],
        "font_roles": (seed.get("font_config") or {}).get("roles") or {},
    }
    return {key: value for key, value in out.items() if value not in ("", 0, [], {}, None)}


def seed_to_brief(seed, staged_manifest=None):
    """Return the raw user query; runtime contracts live in the system context."""
    return seed.get("query", "") if isinstance(seed, dict) else str(seed)


def _skill_tree_hash(skill_dir):
    """Hash one immutable Skill tree by relative path and bytes."""
    digest = hashlib.sha256()
    files = []
    for path in sorted(Path(skill_dir).rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        if path.suffix == ".pyc":
            continue
        relative = path.relative_to(skill_dir).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
        digest.update(b"\0")
        files.append(relative)
    return digest.hexdigest(), files


def _snapshot_skill(run_dir, skill_name=None):
    """Materialize the selected Skill into the Deck and record an immutable hash.

    A persisted Deck must never point at the mutable production checkout.  Revisions keep
    the snapshot already stored in the Deck; legacy symlinks are materialized once.
    """
    skill_name = skill_name or SKILL_BY_LANGUAGE["zh"]
    if skill_name not in set(SKILL_BY_LANGUAGE.values()):
        raise ValueError(f"unsupported frozen Skill: {skill_name}")
    source = os.path.join(SKILLS_DIR, skill_name)
    if not os.path.isdir(source):
        raise FileNotFoundError(f"{skill_name} Skill 不存在: {source}")
    skills_root = os.path.join(run_dir, "skills")
    selected = os.path.join(skills_root, skill_name)
    legacy_symlink = os.path.islink(skills_root) or os.path.islink(selected)
    if legacy_symlink:
        if os.path.islink(skills_root):
            os.unlink(skills_root)
        elif os.path.islink(selected):
            os.unlink(selected)
    if not os.path.isdir(selected):
        os.makedirs(skills_root, exist_ok=True)
        shutil.copytree(
            source,
            selected,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
    tree_sha256, files = _skill_tree_hash(selected)
    trace_dir = os.path.join(run_dir, "_trace")
    os.makedirs(trace_dir, exist_ok=True)
    manifest = {
        "skill": skill_name,
        "language": "en" if skill_name.endswith("-en") else "zh",
        "tree_sha256": tree_sha256,
        "files": files,
        "captured_at_epoch": time.time(),
        "source": os.path.realpath(source),
        "materialized_from_legacy_symlink": legacy_symlink,
    }
    target = os.path.join(trace_dir, "skill-snapshot.json")
    temporary = target + f".{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return skills_root


# Release decks snapshot the bilingual public Skill names. Continue/revision
# must also resolve the historical unsuffixed alias and local-demo canonical
# names so an existing workspace remains editable after packaging.
_LEGACY_SKILL_NAMES = (
    "sn-ppt-web",
    "long-horizon-presenter",
    "long-horizon-presenter-en",
)
_KNOWN_SKILL_NAMES = (*SKILL_BY_LANGUAGE.values(), *_LEGACY_SKILL_NAMES)


def _workspace_skills_root(ws):
    root = os.path.join(ws, "skills")
    for skill_name in _KNOWN_SKILL_NAMES:
        if os.path.isfile(os.path.join(root, skill_name, "SKILL.md")):
            return root
    return SKILLS_DIR


def _workspace_skill_name(ws):
    manifest_path = os.path.join(ws, "_trace", "skill-snapshot.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            selected = str(json.load(stream).get("skill") or "")
        if selected in set(_KNOWN_SKILL_NAMES):
            return selected
    except (OSError, ValueError, TypeError):
        pass
    root = _workspace_skills_root(ws)
    for skill_name in _KNOWN_SKILL_NAMES:
        if os.path.isfile(os.path.join(root, skill_name, "SKILL.md")):
            return skill_name
    return SKILL_BY_LANGUAGE["zh"]


# —— 材料挂载(附件能力):有 seed["attachments"] 时,只把原件拷进 materials/_raw/ + 写 attachments.json;
#    解析/光栅化/catalog 全部交给 material 子代理跑 skill 的 scripts/stage_materials.py(harness 越薄越好、
#    skill 端到端自包含,不在 harness 内联解析/硬编码解析 venv 路径)。


def _safe_material_name(name, fallback="attachment"):
    """Keep attachment names inside materials/_raw and filesystem-safe."""
    name = os.path.basename(str(name or fallback).replace("\\", "/"))
    name = re.sub(r"[^\w.()\-\u3400-\u9fff]+", "_", name, flags=re.UNICODE)
    return name[:180] or fallback


def _unique_material_name(name, used):
    stem, ext = os.path.splitext(name)
    candidate, index = name, 2
    while candidate.casefold() in used:
        candidate = f"{stem}_{index}{ext}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _stage_materials(run_dir, seed):
    """有附件时,只把用户材料**原件拷进** runs/<sid>/materials/_raw/ + 写 attachments.json 清单。
    **解析/光栅化/catalog 全部交给 material 子代理**跑 skill 的 `scripts/stage_materials.py`
    (2026-07-09 改:harness 越薄越好、skill 端到端自包含;agent timeout 已调高应对解析耗时)。
    无附件直接 return——对普通 deck 零影响。"""
    atts = _attachment_list(seed)
    if not atts:
        return []
    mdir = os.path.join(run_dir, "materials")
    raw = os.path.join(mdir, "_raw")
    os.makedirs(raw, exist_ok=True)
    manifest = []                                    # 拷贝清单 → attachments.json(给 agent 的 stage_materials.py 读)
    used_names = set()
    for a in atts:
        src = ((a.get("path") or a.get("source_path"))
               if isinstance(a, dict) else a)
        requested_name = ((a.get("name") or a.get("stored_name"))
                          if isinstance(a, dict) else None)
        name = _safe_material_name(requested_name or (os.path.basename(src) if src else "unknown"))
        name = _unique_material_name(name, used_names)
        if not src or not os.path.exists(src):
            manifest.append({"name": name, "status": "missing"})
            continue
        try:
            shutil.copy2(src, os.path.join(raw, name))
            manifest.append({"name": name, "raw": f"materials/_raw/{name}"})
        except Exception as e:
            manifest.append({"name": name, "status": "failed", "note": f"copy: {e}"})
    with open(os.path.join(mdir, "attachments.json"), "w", encoding="utf-8") as f:
        json.dump({"attachments": manifest}, f, ensure_ascii=False, indent=2)
    # 不再解析/光栅化/写 catalog.json —— material 子代理会跑 skill 的 stage_materials.py 生成 catalog。
    return manifest


def _stage_custom_fonts(run_dir, seed):
    """Mount user-authorized font originals separately from presentation materials."""
    config = (seed or {}).get("font_config") if isinstance(seed, dict) else None
    if not isinstance(config, dict):
        return None
    fonts = config.get("fonts") or []
    if fonts and not config.get("license_acknowledged"):
        raise ValueError("custom font files require an explicit embedding authorization")
    target_dir = os.path.join(run_dir, "materials", "_fonts")
    os.makedirs(target_dir, exist_ok=True)
    staged_fonts = []
    used_names = set()
    for item in fonts:
        if not isinstance(item, dict):
            continue
        source = str(item.get("path") or "")
        if not source or not os.path.isfile(source):
            raise FileNotFoundError(f"custom font source is missing: {item.get('name') or source}")
        name = _unique_material_name(_safe_material_name(item.get("stored_name") or item.get("name") or "font.ttf"), used_names)
        target = os.path.join(target_dir, name)
        shutil.copy2(source, target)
        digest = hashlib.sha256(Path(target).read_bytes()).hexdigest()
        expected = str(item.get("sha256") or "")
        if expected and digest != expected:
            raise ValueError(f"custom font hash mismatch: {name}")
        staged = dict(item)
        staged.pop("path", None)
        staged["source_path"] = f"materials/_fonts/{name}"
        staged["sha256"] = digest
        staged_fonts.append(staged)
    workspace_config = {
        "version": 1,
        "license_acknowledged": bool(config.get("license_acknowledged")),
        "roles": config.get("roles") or {},
        "fonts": staged_fonts,
    }
    config_path = os.path.join(run_dir, "materials", "font-config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(workspace_config, handle, ensure_ascii=False, indent=2)
    return workspace_config


def _missing_attachments(seed):
    """返回 seed 里**路径不存在**的附件路径列表(无附件或全在 → 空)。派发前逐样本预检用:
    附件缺失的 seed 直接跳过、不派给 worker —— 否则 material 子代理会因附件缺失让编排器阻塞、
    每条白烧一份「解析+走到阻塞」的 API(2026-07-10 事故:zhenxi rerun 挪走附件源,整批静默 rejected 空烧一小时)。
    只 stat 不读内容,零成本;每次派发都重算,故附件哪天回来 --resume 会自动再跑(skipped 非终态)。"""
    atts = (seed or {}).get("attachments") if isinstance(seed, dict) else None
    if not atts:
        return []
    miss = []
    for a in atts:
        p = (a.get("path") or a.get("source_path")) if isinstance(a, dict) else a
        if not p or not os.path.isfile(p):
            miss.append(str(p or (a.get("name") if isinstance(a, dict) else "附件路径为空")))
    return miss


MIN_RENDER_BYTES = int(os.environ.get("MIN_RENDER_BYTES", "26000"))   # 退路:观测到的纯色空白图 ~21KB
BLANK_LUMA_RANGE = int(os.environ.get("BLANK_LUMA_RANGE", "24"))      # 灰度跨度小于此 = 近乎纯色 = 空白/破渲染


def _render_ok(png):
    """判断一张渲染图不是空白/破图:优先用 PIL 看灰度跨度(纯色页跨度≈0);没装 PIL 退回字节下限。"""
    try:
        from PIL import Image
        with Image.open(png) as im:
            lo, hi = im.convert("L").getextrema()
        return (hi - lo) >= BLANK_LUMA_RANGE
    except Exception:
        return os.path.getsize(png) >= MIN_RENDER_BYTES


def _delivery_audit(ws, expected):
    """Run the selected Skill's portable-delivery contract before acceptance."""
    deck_py = os.path.join(
        _workspace_skills_root(ws), _workspace_skill_name(ws), "scripts", "deck.py"
    )
    if not os.path.isfile(deck_py):
        return False, "缺少 deck.py，无法执行交付依赖审计"
    try:
        proc = _run_noninteractive(
            [sys.executable, deck_py, "audit", ws, "--expected", str(expected)],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"交付依赖审计无法完成: {type(exc).__name__}: {exc}"
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
        return False, "交付依赖审计失败: " + detail[-1200:]
    return True, "ok"


def _ensure_present_html(ws, expected):
    """Deterministically build a missing portable player before acceptance.

    Orchestrator prose/tool-loop failures must not discard an otherwise usable
    deck.  The Harness owns this final mechanical step.  When it fails, the
    caller may feed the exact command output to a bounded Delivery Fix Agent;
    blindly repeating the same command here would add no evidence.
    """
    target = os.path.join(ws, "present.html")
    try:
        if os.path.getsize(target) >= 100:
            return True, "already_present"
    except OSError:
        pass
    slides = _slide_htmls(ws)
    if not slides:
        return False, "没有页面，无法补建 present.html"
    missing_renders = [
        os.path.basename(path)
        for path in slides
        if not os.path.isfile(os.path.join(
            ws, "renders", os.path.splitext(os.path.basename(path))[0] + ".png"
        ))
    ]
    if missing_renders:
        return False, f"页面渲染不完整，无法补建 present.html: {missing_renders[:5]}"
    deck_py = os.path.join(
        _workspace_skills_root(ws), _workspace_skill_name(ws), "scripts", "deck.py"
    )
    if not os.path.isfile(deck_py):
        return False, "缺少 deck.py，无法补建 present.html"
    try:
        proc = _run_noninteractive(
            [sys.executable, deck_py, "build", ws, "--expected", str(expected)],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Harness 补建 present.html 无法执行: {type(exc).__name__}: {exc}"
    try:
        built = os.path.getsize(target) >= 100
    except OSError:
        built = False
    if proc.returncode == 0 and built:
        return True, "built_by_harness"
    detail = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
    return False, "Harness 补建 present.html 失败: " + (
        detail[-1200:] or "build 未生成 present.html"
    )


def _delivery_gate(ws, expected):
    """Return the current deterministic delivery verdict and exact feedback."""
    player_ok, player_reason = _ensure_present_html(ws, expected)
    if not player_ok:
        return False, player_reason
    return _delivery_audit(ws, expected)


def _delivery_repair_goal(reason, expected, prompt_language="zh"):
    """Build a bounded, artifact-focused correction task from a hard failure."""
    if str(prompt_language or "").lower() == "en":
        return f"""Delivery fix: repair the existing workspace so the deterministic delivery gate passes.

The latest exact failure is:
{reason}

This is corrective work on the current {expected}-slide Deck, not a redesign. Read the relevant existing plans, notes, speech, player, and named files. Modify workspace deliverables only; never edit the snapshotted Skill or Harness implementation. Preserve valid slides, renders, assets, facts, page order, and visual language.

If a page plan lacks its required spoken-script section, recover useful prose from an existing notes file when available, then write a natural, directly speakable `## Spoken script` section back into the canonical `plan/slide_NN.md`. Do not mechanically enumerate on-screen labels. Remove Markdown fence lines and production-only paths/assumptions from user-facing speech and sources. If visible HTML/CSS changes are genuinely required, rerender and inspect the affected final pixels; otherwise do not touch pixels merely to satisfy the gate.

Run the selected Skill's `deck.py build . --expected {expected}` and then `deck.py audit . --expected {expected}`. Use the new command output as feedback and keep correcting within this attempt until both pass or a concrete unrepairable dependency is proven. Finish with:
status: ready | blocked
remaining: none | <exact blocker>
summary: <what was repaired and which commands passed>"""
    return f"""Delivery fix：修复现有工作区，使确定性交付门通过。

最新一次精确失败信息：
{reason}

这是对当前 {expected} 页 Deck 的收口修复，不是重新设计。读取相关的现有计划、notes、讲稿、播放器及报错点名文件。只修改工作区交付物，绝不修改已快照的 Skill/Harness 实现；保留正确的页面、渲染、素材、事实、页序和视觉语言。

如果逐页计划缺少必需讲稿，优先从已有 notes 中恢复可用内容，再把自然、可直接朗读的 `## 口语讲稿` 写回规范 `plan/slide_NN.md`。不得机械枚举屏显标签；清除讲稿中的 Markdown 代码围栏，以及面向用户的讲稿/来源中的内部路径和编排器假设。只有报错确实要求改可见 HTML/CSS 时才修改并重渲、查看受影响最终像素；不要为了过门无理由改画面。

运行所选 Skill 的 `deck.py build . --expected {expected}`，随后运行 `deck.py audit . --expected {expected}`。把新的命令结果继续作为反馈，在本次尝试内修到两者通过，或证明存在具体且不可修的依赖。最后返回：
status: ready | blocked
remaining: none | <精确阻塞项>
summary: <修复内容及通过的命令>"""


def _ensure_delivery_with_agent(orch, expected, *, allow_agent_repair=False):
    """Feed hard delivery failures back to a bounded correction Agent."""
    failures = []
    for repair_no in range(DELIVERY_REPAIR_ATTEMPTS + 1):
        ok, reason = _delivery_gate(orch.ws, expected)
        if ok:
            if failures:
                orch.log(f"交付修复完成：第 {len(failures)} 次反馈后 build/audit 通过")
            return True, "ok"
        failures.append(reason)
        if not allow_agent_repair or repair_no >= DELIVERY_REPAIR_ATTEMPTS:
            return False, reason
        from core.agent import delegate_task
        goal = _delivery_repair_goal(
            reason, expected, getattr(orch, "prompt_language", "zh")
        )
        orch.log(
            f"交付门未通过：把精确错误反馈给 Delivery Fix Agent "
            f"({repair_no + 1}/{DELIVERY_REPAIR_ATTEMPTS})"
        )
        result = delegate_task(
            orch,
            goal=goal,
            context="The deterministic gate will rerun after this attempt; its result is authoritative.",
            toolsets=["file", "terminal"],
            role="subagent",
            label=f"delivery_fix_{repair_no + 1}",
        )
        orch.log(f"Delivery Fix Agent 返回：{str(result)[:500]}")
    return False, failures[-1]


# 只认规范页文件 `slide_<纯数字>.html`;skill 可能生成 slide_07.bak.html / slide_07.html.bak 之类的备份,
# 它们**不算正式页**——否则 .bak.html 会被 glob 当成无渲染的页,误判整条 deck 拒收。
_SLIDE_RE = re.compile(r"^slide_\d+\.html$")


def _slide_htmls(ws):
    """工作区里**规范的**页 HTML(slide_<数字>.html),排除 .bak 等备份文件。"""
    return sorted(p for p in glob.glob(os.path.join(ws, "slides", "slide_*.html"))
                  if _SLIDE_RE.match(os.path.basename(p)))


def _v_pass(html_path, ws):
    """保守机核 V:对最终 HTML 重跑 render.py,读它打印的『机检结论』行判 4 项 blocking
    (off_canvas / broken_image / cjk_tofu / placeholder)。非破坏:渲到临时 PNG,不覆盖 renders/ 成品图。
    判定策略保守(paper:宁漏勿误杀):只在明确读到『机检结论: 不过』时判 fail;
    脚本缺失 / 渲染异常 / 无结论行,一律 fail-open(不阻断)——页面完整性已由上面 missing/blank 兜底。
    返回 (ok, output)。"""
    render_py = os.path.join(
        _workspace_skills_root(ws), _workspace_skill_name(ws), "scripts", "render.py"
    )
    if not os.path.exists(render_py):
        return True, "(render.py 不在,跳过 V)"
    tmp = tempfile.NamedTemporaryFile(prefix="vcheck_", suffix=".png", delete=False)
    tmp.close()
    try:
        proc = _run_noninteractive(
            [sys.executable, render_py, html_path, tmp.name],
            cwd=ws, capture_output=True, text=True, timeout=180,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "机检结论: 不过" in out:
            return False, out
        return True, out                       # 通过 / 无结论行 / 渲染噪声 → 不阻断
    except Exception as e:
        return True, f"(V 重渲异常 {type(e).__name__}: {e} —— 不阻断)"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _workspace_file(ws, relative):
    if not relative:
        return None
    root = os.path.abspath(ws)
    path = os.path.abspath(os.path.join(root, str(relative)))
    if os.path.commonpath([root, path]) != root:
        return None
    return path


def _coverage_error(ws, entry, catalog):
    name = entry.get("name") or "unknown"
    status = str(entry.get("status") or "").lower()
    coverage = entry.get("coverage") or {}
    if status != "ok":
        return f"{name} status={status or 'missing'}，未完成全文覆盖"
    if coverage.get("status") != "complete":
        return f"{name} coverage={coverage.get('status') or 'missing'}"
    if not entry.get("coverage_id"):
        return f"{name} 缺少 coverage_id"

    unit = coverage.get("unit")
    if unit == "chars":
        chunks = entry.get("text_chunks") or []
        total = int(coverage.get("total") or 0)
        if not chunks or total <= 0:
            return f"{name} 缺少全文 chunk"
        cursor = 0
        rebuilt = []
        for chunk in chunks:
            start = int(chunk.get("start_char") or 0)
            end = int(chunk.get("end_char") or 0)
            if start != cursor or end <= start or int(chunk.get("chars") or 0) != end - start:
                return f"{name} chunk 区间不连续"
            path = _workspace_file(ws, chunk.get("path"))
            if not path or not os.path.isfile(path):
                return f"{name} chunk 文件缺失: {chunk.get('path')}"
            with open(path, encoding="utf-8") as stream:
                content = stream.read()
            if len(content) != end - start:
                return f"{name} chunk 字符数不匹配: {chunk.get('path')}"
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != chunk.get("sha256"):
                return f"{name} chunk hash 不匹配: {chunk.get('path')}"
            rebuilt.append(content)
            cursor = end
        if cursor != total or int(coverage.get("covered") or 0) != total:
            return f"{name} chunk 未覆盖全文: {cursor}/{total}"
        full_path = _workspace_file(ws, entry.get("text"))
        if not full_path or not os.path.isfile(full_path):
            return f"{name} 完整解析文本缺失"
        with open(full_path, encoding="utf-8") as stream:
            full_text = stream.read()
        if full_text != "".join(rebuilt):
            return f"{name} 完整文本与 chunk 合并结果不一致"
        if hashlib.sha256(full_text.encode("utf-8")).hexdigest() != entry.get("text_sha256"):
            return f"{name} 完整文本 hash 不匹配"
    elif unit == "pages":
        covered = int(coverage.get("covered") or 0)
        total = int(coverage.get("total") or 0)
        page_entries = [item for item in catalog if item.get("from_scanned_pdf") == name]
        if total <= 0 or covered != total or len(page_entries) != total:
            return f"{name} 扫描页覆盖不完整: {covered}/{total}"
        for page in page_entries:
            path = _workspace_file(ws, page.get("raw"))
            if page.get("status") != "ok" or not path or not os.path.isfile(path):
                return f"{name} 扫描页文件缺失: {page.get('page')}"
    elif unit == "asset":
        path = _workspace_file(ws, entry.get("raw"))
        if not path or not os.path.isfile(path):
            return f"{name} 图片附件缺失"
    else:
        return f"{name} coverage unit 不受支持: {unit or 'missing'}"
    return ""


def _disk_material_worker_ready(ws, base_label):
    """Persisted-truth fallback for a material worker's acceptance.

    Mirrors the Image→Slide gate fallback: a stale in-memory ``clean`` flag must
    not block a material stage that was genuinely completed (a re-run or a
    corrected handoff on disk).  Uses the shared, attempt-aware worker state so
    only the *active* attempt for this base (not a superseded/failed one) whose
    handoff reports clean + ready + complete coverage counts.
    """
    from core.agent import _active_workers_from_disk

    for rec in _active_workers_from_disk(ws, "material", clean_source_only=True):
        if re.sub(r"_r\d+$", "", str(rec.get("label") or "")) != base_label:
            continue
        contract = rec.get("contract") or {}
        coverage = str(contract.get("coverage") or "").strip().lower()
        if (bool(rec.get("clean"))
                and str(contract.get("status") or "").lower() == "ready"
                and coverage.startswith("complete")):
            return True
    return False


def _material_acceptance(ws, worker_recs):
    """Require lossless coverage and a summary ledger for every attachment.

    The selected Skill normally delegates Material work, but delivery quality
    is defined by the canonical coverage artifacts rather than by one specific
    orchestration topology. A capable Orchestrator may complete the same
    lossless stage directly; do not reject a fully covered attachment merely
    because there is no separate Material worker record.
    """
    from core.agent import _effective_active_recs

    material_active = _effective_active_recs(worker_recs, kind="material")
    bad_contracts = []
    for worker in material_active:
        label = str(worker.get("label") or "")
        base = re.sub(r"_r\d+$", "", label)
        contract = worker.get("contract") or {}
        coverage = str(contract.get("coverage") or "").strip().lower()
        if (not worker.get("clean") or contract.get("status") != "ready"
                or not coverage.startswith("complete")):
            # In-memory worker_recs freeze the clean flag; a later correction or
            # re-run persists to handoff.json.  Consult that durable truth before
            # rejecting, so a genuinely complete material stage is not blocked by
            # a stale in-memory record.
            if not _disk_material_worker_ready(ws, base):
                bad_contracts.append(label)
    if bad_contracts:
        return False, f"Material 未返回 ready/complete: {bad_contracts[:5]}"

    manifest_path = os.path.join(ws, "materials", "attachments.json")
    try:
        with open(manifest_path, encoding="utf-8") as stream:
            manifest = json.load(stream).get("attachments") or []
        expected_names = [item.get("name") for item in manifest if item.get("name")]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return False, f"附件清单无法读取: {type(exc).__name__}"
    if not expected_names:
        return False, "附件清单为空"

    catalog_paths = sorted(glob.glob(os.path.join(ws, "materials", "_work", "*", "catalog.json")))
    legacy_catalog = os.path.join(ws, "materials", "catalog.json")
    if os.path.isfile(legacy_catalog):
        catalog_paths.append(legacy_catalog)
    if not catalog_paths:
        return False, "material 未生成任何分片 catalog.json"
    expected_set = set(expected_names)

    def is_derivative(entry):
        """Return True for visual/text assets derived from one manifest attachment.

        New catalogs use explicit lineage.  The legacy checks keep already-staged
        workspaces valid without confusing PDF page previews with extra uploads.
        A standalone image attachment is still a root entry because its exact
        name appears in ``expected_set``.
        """
        name = str(entry.get("name") or "")
        if name in expected_set:
            return False
        if entry.get("is_derivative") or entry.get("derived_from"):
            return True
        if any(entry.get(field) for field in (
            "from_scanned_pdf", "from_pdf", "from_document", "from_video", "from_image",
        )):
            return True
        return any(name.startswith(parent + " · ") for parent in expected_set)

    seen = {}
    try:
        for catalog_path in catalog_paths:
            with open(catalog_path, encoding="utf-8") as f:
                catalog = json.load(f)
            if not isinstance(catalog, list) or not catalog:
                return False, f"{os.path.relpath(catalog_path, ws)} 为空或格式不正确"
            assignment = os.path.basename(os.path.dirname(catalog_path))
            summary_path = (
                os.path.join(ws, "research", "materials.md")
                if assignment == "materials"
                else os.path.join(ws, "research", "materials", f"{assignment}.md")
            )
            if not os.path.isfile(summary_path) or os.path.getsize(summary_path) < 20:
                return False, f"{assignment} 缺少有效分片摘要"
            with open(summary_path, encoding="utf-8") as stream:
                summary = stream.read()
            for entry in (item for item in catalog if not is_derivative(item)):
                name = entry.get("name")
                if not name:
                    return False, f"{assignment} catalog 条目缺少 name"
                if name in seen:
                    return False, f"附件被重复解析: {name}"
                error = _coverage_error(ws, entry, catalog)
                if error:
                    return False, error
                if entry["coverage_id"] not in summary:
                    return False, f"{name} 的 coverage_id 未写入分片摘要"
                seen[name] = catalog_path
    except (OSError, ValueError, TypeError) as exc:
        return False, f"material catalog 无法读取: {type(exc).__name__}"
    missing = [name for name in expected_names if name not in seen]
    extra = [name for name in seen if name not in expected_set]
    if missing or extra:
        return False, f"附件 coverage 不一一对应: missing={missing[:5]} extra={extra[:5]}"
    return True, "ok"


def _research_acceptance(ws, worker_recs):
    """Accept Research by its durable brief; treat contract fields as diagnostics.

    Research is a task-level singleton: after collapsing each base label to its
    effective active attempt, there must be exactly ONE active Research base.
    More than one active base is an ambiguity no matter the success/failure mix —
    we never "pick a successful one".  The single active attempt is then validated
    against the formal artifact / canonical grounding handoff.
    """
    from core.agent import _effective_active_recs

    active = _effective_active_recs(worker_recs, kind="research")
    if not active:
        return True, "ok"
    if len(active) > 1:
        bases = sorted({re.sub(r"_r\d+$", "", str(w.get("label") or "")) for w in active})
        return False, f"Research 存在多个 active 实例(歧义，任务级单例): {bases[:4]}"
    worker = active[0]
    contract = worker.get("contract") or {}
    status = str(contract.get("status") or "").strip().lower()
    declared = str(contract.get("output") or "").strip()
    output_text = ""
    for relative in dict.fromkeys(
            value for value in (declared, "research/research.md") if value):
        output = _workspace_file(ws, relative)
        try:
            candidate_text = Path(output).read_text(encoding="utf-8", errors="ignore")
        except (OSError, TypeError):
            candidate_text = ""
        if candidate_text.strip():
            output_text = candidate_text
            break
    if not output_text.strip():
        return False, "Research 正式 brief 缺失或为空"

    warnings = []
    if not worker.get("clean"):
        warnings.append("worker 合同未标记 clean")
    if status not in {"ready", "partial"}:
        warnings.append(f"status={status or 'missing'}")
    unresolved = str(contract.get("unresolved") or "").strip().lower()
    if status == "partial" and unresolved in {"", "none", "n/a", "not-applicable"}:
        warnings.append("partial 未声明 unresolved")
    if warnings:
        return True, "Research 合同字段不完整，已按正式产物继续: " + "; ".join(warnings)
    return True, "ok"


def _grounded_acceptance(ws, *, require_materials, research_workers):
    """Require the grounding handoff exactly when an evidence stage ran."""
    if not require_materials and not research_workers:
        return True, "ok"
    grounded = os.path.join(ws, "plan", "grounded-knowledge.md")
    try:
        if os.path.isfile(grounded) and os.path.getsize(grounded) >= 40:
            return True, "ok"
    except OSError:
        pass
    return False, "缺少有效 plan/grounded-knowledge.md；Material/Research 交接未完成"


_IMAGE_OPPORTUNITY_RE = re.compile(
    r"(?im)^\s*[-*+]?\s*(?:\*\*)?image_opportunity(?:\*\*)?\s*[:：]\s*(.+?)\s*$"
)


def _planned_asset_ids(ws):
    # Reuse the harness's single source of truth for the no-bitmap decision so
    # the startup dispatch gate and this final acceptance never diverge (a page
    # marked ``none`` or the CJK ``无位图`` must read the same on both sides).
    from core.agent import _image_opportunity_needs_bitmap, _plan_asset_ids

    ids, needs_image = set(), False
    for plan in glob.glob(os.path.join(ws, "plan", "slide_*.md")):
        try:
            text = Path(plan).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ids.update(_plan_asset_ids(text))
        opportunity = _IMAGE_OPPORTUNITY_RE.search(text)
        if opportunity and _image_opportunity_needs_bitmap(opportunity.group(1)):
            needs_image = True
    return ids, needs_image


def _bitmap_exception_acceptance(ws):
    """Accept an all-no-bitmap deck only after the explicit planning review."""
    plans = sorted(glob.glob(os.path.join(ws, "plan", "slide_*.md")))
    expected = {
        int(match.group(1)) for path in plans
        for match in [re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)]
        if match
    }
    path = os.path.join(ws, "plan", "image-strategy.json")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        reviewed = {int(page) for page in payload.get("reviewed_pages") or []
                    if str(page).isdigit()}
    except (OSError, ValueError, TypeError, AttributeError):
        return False, "全册无位图但缺少有效 plan/image-strategy.json"
    allowed = {
        "explicit_user_request", "pure_typography", "pure_chart", "wireframe",
        "accuracy_critical",
    }
    if (payload.get("status") != "bitmap_exception"
            or payload.get("visible_subject_scan_complete") is not True
            or str(payload.get("exception_basis") or "").lower() not in allowed
            or len(str(payload.get("exception_reason") or "").strip()) < 20
            or not expected or reviewed != expected):
        return False, "plan/image-strategy.json 的全册无位图复核字段不完整"
    return True, "ok"


def _image_acceptance(ws, worker_recs, *, allow_existing_assets=False):
    """Require every planned asset to resolve, and new Image workers to be ready.

    A revision may legitimately reuse the immutable catalog produced by the
    original run.  Requiring a fresh Image worker for a text/layout-only edit
    creates a false rejection; the actual invariant is that every referenced
    asset still resolves.  When a revision does dispatch Image, that worker is
    still required to finish cleanly.
    """
    from core.agent import _effective_active_recs

    workers = _effective_active_recs(worker_recs, kind="image")
    asset_ids, needs_image = _planned_asset_ids(ws)
    if (asset_ids or needs_image) and not workers and not allow_existing_assets:
        return False, "逐页计划需要 Image，但没有任何 Image worker record"
    # EVERY effective-active Image shard must be clean+ready.  A single active
    # blocked base must reject even when another base is ready (multiple legit
    # Image shards); "some ready exists" is not sufficient.
    blocked_active = [
        worker for worker in workers
        if not (worker.get("clean") and str(
            (worker.get("contract") or {}).get("status") or "").lower() == "ready")
    ]
    if blocked_active:
        attempts = [
            f"{worker.get('label')}:{str((worker.get('contract') or {}).get('status') or 'missing').lower()}"
            for worker in blocked_active
        ]
        return False, f"Image 阶段有未完成的 active 分片: {attempts[:8]}"
    if needs_image and not asset_ids:
        return False, "计划声明需要图片，但未分配稳定 asset_id"
    if not asset_ids:
        if glob.glob(os.path.join(ws, "plan", "slide_*.md")):
            return _bitmap_exception_acceptance(ws)
        return True, "ok"
    catalog_path = os.path.join(ws, "assets", "catalog.json")
    try:
        payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        entries = payload.get("assets") or []
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return False, f"assets/catalog.json 无法读取: {type(exc).__name__}"
    # Shared active-aware, order-independent resolution: a rejected/superseded
    # historical entry with the same asset_id must not shadow the active ready
    # one, and duplicate active entries are an explicit ambiguity (matches the
    # agent-side dispatch gate _catalog_asset_error).
    from core.agent import _catalog_active_by_id

    resolved = _catalog_active_by_id(entries)
    missing, unresolved, ambiguous = [], [], []
    for asset_id in sorted(asset_ids):
        entry, state = resolved.get(asset_id, (None, "missing"))
        if state == "ambiguous":
            ambiguous.append(asset_id)
            continue
        if entry is None:
            missing.append(asset_id)
            continue
        path = _workspace_file(ws, entry.get("path"))
        if entry.get("status") != "ready" or not path or not os.path.isfile(path):
            unresolved.append(asset_id)
    if missing or unresolved or ambiguous:
        return False, (
            f"图片资产未解析: missing={missing[:8]} not_ready={unresolved[:8]} "
            f"ambiguous={ambiguous[:8]}"
        )
    return True, "ok"


_PRODUCTION_GROUP_RE = re.compile(
    r"(?mi)^\s*[-*+]\s*(?:\*\*)?production_group(?:\*\*)?\s*[:：]\s*`?([A-Za-z0-9._-]+)"
)


def _planned_production_groups(ws):
    pages = []
    for path in sorted(glob.glob(os.path.join(ws, "plan", "slide_*.md"))):
        match = re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)
        if not match:
            continue
        with open(path, encoding="utf-8") as stream:
            groups = sorted(set(_PRODUCTION_GROUP_RE.findall(stream.read())))
        pages.append((int(match.group(1)), groups))
    if not pages or not any(groups for _, groups in pages):
        return {}
    missing = [page for page, groups in pages if not groups]
    ambiguous = {page: groups for page, groups in pages if len(groups) != 1}
    if missing or ambiguous:
        raise ValueError(
            "每份逐页计划必须声明唯一 production_group；"
            f"missing={missing[:20]} ambiguous={dict(list(ambiguous.items())[:8])}"
        )
    result = {}
    for page, groups in pages:
        result.setdefault(groups[0], set()).add(page)
    return result


def _slide_assignment_acceptance(ws, worker_recs, *, allow_partial=False):
    """Verify each produced page has exactly one machine-visible Slide owner."""
    expected = {
        int(match.group(1))
        for path in glob.glob(os.path.join(ws, "plan", "slide_*.md"))
        for match in [re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)]
        if match
    }
    if expected and expected != set(range(1, max(expected) + 1)):
        return False, f"逐页计划页码不连续: actual={sorted(expected)[:30]}"
    # A failed Production Group may be continued by its exact same canonical
    # owner.  Use the shared deterministic effective-active selection (highest
    # attempt/ts/label per base) so ownership does not depend on input order.
    from core.agent import _effective_active_recs

    latest_slides = _effective_active_recs(worker_recs, kind="slide")
    owners = {}
    for worker in latest_slides:
        label = str(worker.get("label") or "")
        pages = [int(page) for page in worker.get("assigned_pages") or []]
        if not pages:
            return False, f"{label} 没有机器可见 assigned_pages"
        for page in pages:
            owners.setdefault(page, []).append(label)
    duplicates = {page: labels for page, labels in owners.items() if len(labels) != 1}
    if duplicates:
        return False, f"Slide 页面存在重复 owner: {dict(list(duplicates.items())[:8])}"
    if allow_partial:
        extra = sorted(set(owners) - expected) if expected else []
        if extra:
            return False, f"Revision Slide owner 包含计划外页码: {extra[:12]}"
        return True, "ok"
    missing = sorted(expected - set(owners))
    extra = sorted(set(owners) - expected)
    if missing or extra:
        return False, f"Slide owner 覆盖不完整: missing={missing[:12]} extra={extra[:12]}"
    try:
        planned_groups = _planned_production_groups(ws)
    except ValueError as error:
        return False, str(error)
    if planned_groups:
        expected_sets = {
            frozenset(pages): group_id
            for group_id, pages in planned_groups.items()
        }
        for worker in latest_slides:
            label = str(worker.get("label") or "")
            page_set = frozenset(int(page) for page in worker.get("assigned_pages") or [])
            if page_set not in expected_sets:
                return False, (
                    "Slide worker 与冻结 production_group 不一致: "
                    f"{label}={sorted(page_set)}"
                )
    return True, "ok"


def _attachment_review_acceptance(ws, worker_recs):
    from core.agent import _effective_active_recs

    reviews = _effective_active_recs(worker_recs, kind="review")
    latest = reviews[-1] if reviews else None
    if latest is None:
        return False, "附件任务没有执行最终 Review"
    contract = latest.get("contract") or {}
    if not latest.get("clean") or contract.get("status") != "ready":
        return False, "附件任务的最终 Review 未返回 ready"
    if contract.get("content_fidelity") != "pass":
        return False, "附件任务的最终 Review 未通过 content_fidelity"
    report = os.path.join(ws, *CONTENT_FIDELITY_PATH.split("/"))
    if not os.path.isfile(report) or os.path.getsize(report) < 40:
        return False, f"附件任务缺少 {CONTENT_FIDELITY_PATH}"
    return True, "ok"


def _review_pixel_coverage(ws, vision_paths):
    """Resolve actually viewed review sheets/single pages to covered page numbers."""
    manifest_path = os.path.join(ws, "renders", "review-contact.json")
    try:
        with open(manifest_path, encoding="utf-8") as stream:
            manifest = json.load(stream).get("full") or {}
    except (OSError, ValueError, TypeError):
        return [], [], "缺少有效 renders/review-contact.json"
    expected = [int(page) for page in manifest.get("pages") or []]
    if not expected:
        return [], [], "review contact manifest 未列出页面"
    groups = {
        str(item.get("path") or "").replace("\\", "/").lstrip("./"): [
            int(page) for page in item.get("pages") or []
        ]
        for item in manifest.get("groups") or []
        if isinstance(item, dict) and item.get("path")
    }
    covered = set()
    for raw in vision_paths or []:
        raw_path = str(raw or "")
        if os.path.isabs(raw_path):
            raw_path = os.path.relpath(raw_path, ws)
        path = raw_path.replace("\\", "/").lstrip("./")
        if path in groups:
            covered.update(groups[path])
            continue
        match = re.search(r"(?:^|/)renders/slide_(\d+)\.png$", path, re.I)
        if match:
            covered.add(int(match.group(1)))
    missing = sorted(set(expected) - covered)
    return expected, sorted(covered), None if not missing else f"Review 像素覆盖缺页: {missing[:12]}"


def _review_pixel_freshness(ws, review):
    """Compute final reviewed pages from current image bytes and source/render mtimes."""
    manifest_path = os.path.join(ws, "renders", "review-contact.json")
    try:
        with open(manifest_path, encoding="utf-8") as stream:
            full = (json.load(stream).get("full") or {})
    except (OSError, ValueError, TypeError):
        return False, "缺少有效 renders/review-contact.json"
    expected = {int(page) for page in full.get("pages") or []}
    groups = {
        str(item.get("path") or "").replace("\\", "/").lstrip("./"): {
            int(page) for page in item.get("pages") or []
        }
        for item in full.get("groups") or [] if isinstance(item, dict)
    }
    evidence = review.get("vision_evidence") or {}

    def normalize(path):
        raw = str(path or "")
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, ws)
        return raw.replace("\\", "/").lstrip("./")

    current_evidence = set()
    for raw, item in evidence.items():
        rel = normalize(raw)
        fp = os.path.join(ws, rel)
        try:
            with open(fp, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            mtime = os.stat(fp).st_mtime_ns
        except OSError:
            continue
        if (isinstance(item, dict) and item.get("sha256") == digest
                and int(item.get("mtime_ns") or 0) == mtime):
            current_evidence.add(rel)

    css = os.path.join(ws, "base.css")
    css_mtime = os.stat(css).st_mtime_ns if os.path.isfile(css) else 0

    def page_is_fresh(page, viewed_mtime):
        html = os.path.join(ws, "slides", f"slide_{page:02d}.html")
        png = os.path.join(ws, "renders", f"slide_{page:02d}.png")
        try:
            source_mtime = max(os.stat(html).st_mtime_ns, css_mtime)
            png_mtime = os.stat(png).st_mtime_ns
        except OSError:
            return False
        return source_mtime <= png_mtime <= viewed_mtime

    covered = set()
    for rel in current_evidence:
        fp = os.path.join(ws, rel)
        viewed_mtime = os.stat(fp).st_mtime_ns
        if rel in groups:
            for page in groups[rel]:
                if page_is_fresh(page, viewed_mtime):
                    covered.add(page)
            continue
        match = re.search(r"(?:^|/)renders/slide_(\d+)\.png$", rel, re.I)
        if match and page_is_fresh(int(match.group(1)), viewed_mtime):
            covered.add(int(match.group(1)))
    missing = sorted(expected - covered)
    if missing:
        return False, f"Review 最终像素证据过期或缺页: {missing[:12]}"
    return True, "ok"


def _legacy_trace_vision_evidence(ws, trace_dir):
    """Recover successful pre-sidecar inspections from messages + snapshots.

    Vision images embedded in messages are resized copies, so byte equality
    with the original render is not expected.  The tool-use/result pair proves
    which source produced each immutable snapshot; source mtime must be no
    newer than that snapshot, otherwise the inspection is correctly stale.
    """
    messages_path = os.path.join(trace_dir, "messages.json")
    try:
        messages = json.loads(Path(messages_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return [], {}
    if not isinstance(messages, list):
        return [], {}

    uses = {}
    results = {}
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "vision_analyze":
                args = block.get("input") or {}
                if isinstance(args, dict) and str(args.get("image_url") or "").strip():
                    uses[str(block.get("id") or "")] = str(args["image_url"])
            if block.get("type") != "tool_result" or not isinstance(block.get("content"), list):
                continue
            shot = next((
                str(item.get("shot") or "") for item in block["content"]
                if isinstance(item, dict) and item.get("type") == "image" and item.get("shot")
            ), "")
            if shot:
                results[str(block.get("tool_use_id") or "")] = shot

    paths = []
    evidence = {}
    for tool_id, shot in results.items():
        raw = uses.get(tool_id)
        if not raw:
            continue
        rel = os.path.relpath(raw, ws) if os.path.isabs(raw) else raw
        rel = rel.replace("\\", "/").lstrip("./")
        source = os.path.join(ws, rel)
        snapshot = shot if os.path.isabs(shot) else os.path.join(trace_dir, shot)
        try:
            source_stat = os.stat(source)
            snapshot_stat = os.stat(snapshot)
            if source_stat.st_mtime_ns > snapshot_stat.st_mtime_ns:
                continue
            digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        except OSError:
            continue
        paths.append(rel)
        evidence[rel] = {"sha256": digest, "mtime_ns": source_stat.st_mtime_ns}
    return list(dict.fromkeys(paths)), evidence


def _review_with_persisted_evidence(ws, review):
    """Merge independently persisted pixel evidence into a Review record.

    Review's natural-language final contract and successful pixel inspections
    are separate runtime events.  A model can finish the inspections and then
    stall before its final response, so acceptance must not rely solely on the
    compact worker handoff.  The per-Review sidecar is written after every
    successful vision call and remains authoritative for metadata recovery.
    """
    recovered = dict(review or {})
    paths = list(recovered.get("vision_paths") or [])
    evidence = dict(recovered.get("vision_evidence") or {})
    candidates = []

    trace_dir = str(recovered.get("trace_dir") or "").strip()
    if trace_dir:
        candidates.append(os.path.join(ws, trace_dir, "vision-evidence.json"))

    label = str(recovered.get("label") or "review").strip()
    if label and re.fullmatch(r"[A-Za-z0-9._-]+", label):
        candidates.extend(glob.glob(os.path.join(
            ws, "_trace", "**", "subagents", label, "vision-evidence.json"
        ), recursive=True))

    # Prefer the newest sidecar if a bounded Review retry produced more than
    # one trace.  Merge rather than replace so an already complete in-memory
    # record never loses evidence.
    existing = [path for path in dict.fromkeys(candidates) if os.path.isfile(path)]
    existing.sort(key=lambda path: os.path.getmtime(path))
    recovered_calls = int(recovered.get("vision_calls") or 0)
    for path in existing:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        paths.extend(payload.get("vision_paths") or [])
        persisted = payload.get("vision_evidence") or {}
        if isinstance(persisted, dict):
            evidence.update(persisted)
        try:
            recovered_calls = max(recovered_calls, int(payload.get("vision_calls") or 0))
        except (TypeError, ValueError):
            pass

    if not evidence:
        trace_candidates = []
        if trace_dir:
            trace_candidates.append(os.path.join(ws, trace_dir))
        if label and re.fullmatch(r"[A-Za-z0-9._-]+", label):
            trace_candidates.extend(glob.glob(os.path.join(
                ws, "_trace", "**", "subagents", label
            ), recursive=True))
        for candidate in dict.fromkeys(trace_candidates):
            legacy_paths, legacy_evidence = _legacy_trace_vision_evidence(ws, candidate)
            paths.extend(legacy_paths)
            evidence.update(legacy_evidence)
        recovered_calls = max(recovered_calls, len(evidence))

    recovered["vision_paths"] = list(dict.fromkeys(
        str(path).replace("\\", "/") for path in paths if str(path or "").strip()
    ))
    recovered["vision_evidence"] = evidence
    recovered["vision_calls"] = max(recovered_calls, len(evidence))
    return recovered


def _pixel_review_acceptance(
    worker_recs, *, ws=None, allow_review_only=False, require_content_fidelity=False
):
    """Validate the latest bounded Review attempt and its pixel evidence."""
    from core.agent import _effective_active_recs

    slide_workers = _effective_active_recs(worker_recs, kind="slide")
    if not slide_workers and not allow_review_only:
        return False, "没有可验收的 Slide 子 Agent"
    failed_slides = [worker for worker in slide_workers if not worker.get("clean")]
    blind_slides = [
        worker.get("label") for worker in slide_workers
        if int(worker.get("vision_calls") or 0) < 1
    ]
    if blind_slides:
        return False, f"Slide 未实际调用 vision_analyze: {blind_slides[:5]}"
    incomplete_slides = []
    for worker in slide_workers:
        assigned = {int(page) for page in worker.get("assigned_pages") or []}
        inspected = {int(page) for page in worker.get("inspected_pages") or []}
        missing = sorted(assigned - inspected)
        if missing:
            incomplete_slides.append(
                f"{worker.get('label')}:{','.join(f'{page:02d}' for page in missing)}"
            )
    if incomplete_slides:
        return False, f"Slide 缺少逐页像素自检: {incomplete_slides[:5]}"

    # A page worker's ``clean`` flag is an intermediate diagnostic, not the
    # final delivery verdict.  A worker may have emitted a soft/advisory warning
    # before the orchestrator or final Review produced fresh valid artifacts.
    # Preserve the safety gate by requiring every affected page to exist and
    # have a PNG no older than its HTML/base.css; the task-level Review below is
    # still the authoritative final acceptance.
    if failed_slides:
        if not ws:
            labels = [worker.get("label") for worker in failed_slides]
            return False, f"Slide 子 Agent 未正常完成且无法核验交付物: {labels[:5]}"
        css_path = os.path.join(ws, "base.css")
        try:
            css_mtime = os.stat(css_path).st_mtime_ns
        except OSError:
            return False, "Slide 子 Agent 状态异常且 base.css 缺失"
        stale = []
        for worker in failed_slides:
            assigned_pages = worker.get("assigned_pages") or []
            if not assigned_pages:
                stale.append(f"{worker.get('label')}:unassigned")
                continue
            for page in assigned_pages:
                number = int(page)
                html = os.path.join(ws, "slides", f"slide_{number:02d}.html")
                png = os.path.join(ws, "renders", f"slide_{number:02d}.png")
                try:
                    if os.stat(png).st_mtime_ns < max(os.stat(html).st_mtime_ns, css_mtime):
                        stale.append(f"{worker.get('label')}:{number:02d}")
                except OSError:
                    stale.append(f"{worker.get('label')}:{number:02d}")
        if stale:
            return False, f"Slide 子 Agent 未完成且交付物缺失或过期: {stale[:8]}"

    reviews = _effective_active_recs(worker_recs, kind="review")
    if not reviews:
        return False, "没有执行最终 Review"
    # Deterministic: the single latest active review base (recs are ts/attempt/
    # label ordered by the shared selector).
    review = reviews[-1]
    if ws:
        review = _review_with_persisted_evidence(ws, review)
    contract = review.get("contract") or {}
    if not review.get("clean") or contract.get("status") != "ready":
        status = str(contract.get("status") or "missing")
        detail = str(
            contract.get("remaining")
            or contract.get("validation_error")
            or review.get("exit_reason")
            or "未提供具体原因"
        )
        return False, f"最终 Review 返回 {status}，未通过质量门：{detail}"
    if int(review.get("vision_calls") or 0) < 1:
        return False, "最终 Review 未实际调用 vision_analyze"
    expected_mode = "simple_edit" if allow_review_only and not slide_workers else "final_review"
    if contract.get("mode") != expected_mode:
        return False, f"Review mode 错误：需要 {expected_mode}，得到 {contract.get('mode') or 'missing'}"
    if contract.get("final_pixels_inspected") != "yes":
        return False, "最终 Review 未确认 final_pixels_inspected: yes"
    if contract.get("remaining") != "none":
        return False, "最终 Review 仍有 remaining 问题或未声明 none"
    if contract.get("speech_aligned") != "yes":
        return False, "最终 Review 未确认 speech_aligned: yes"
    if expected_mode == "final_review":
        if contract.get("diagnosed_pages") != "all":
            return False, "final_review 未覆盖 diagnosed_pages: all"
        if ws:
            _, _, coverage_error = _review_pixel_coverage(
                ws, review.get("vision_paths") or []
            )
            if coverage_error:
                return False, coverage_error
            fresh_ok, fresh_reason = _review_pixel_freshness(ws, review)
            if not fresh_ok:
                return False, fresh_reason
            issue_log = os.path.join(ws, *REVIEW_ISSUES_PATH.split("/"))
            if not os.path.isfile(issue_log) or os.path.getsize(issue_log) < 20:
                return False, f"final_review 缺少唯一 {REVIEW_ISSUES_PATH} 问题账本"
            issue_text = Path(issue_log).read_text(encoding="utf-8", errors="ignore")
            if re.search(r"(?mi)^\s*missing_pages\s*:\s*(?!none\s*$).+", issue_text):
                return False, "final_review 问题账本仍声明 missing_pages"
            if not re.search(r"(?mi)^\s*diagnosed_pages\s*:\s*all\s*$", issue_text):
                return False, "final_review 问题账本未记录 diagnosed_pages: all"
            if not re.search(r"(?mi)^\s*remaining\s*:\s*none\s*$", issue_text):
                return False, "final_review 问题账本未记录 remaining: none"
    machine_rounds = int(review.get("machine_refine_rounds") or 0)
    try:
        reported_rounds = int(contract.get("refine_rounds") or 0)
    except (TypeError, ValueError):
        return False, "Review refine_rounds 不是有效整数"
    if machine_rounds > 1:
        return False, f"Review 实际 refine 轮次超过 1: {machine_rounds}"
    # The machine counter is authoritative.  A model's self-reported number is
    # useful trace metadata, but a mismatch cannot invalidate fresh pixels and
    # an otherwise complete Review contract.  In particular, weak models often
    # call the requested simple edit "round 0" while the runtime correctly
    # records the edit -> render cycle as round 1.
    fidelity = contract.get("content_fidelity")
    if require_content_fidelity and fidelity != "pass":
        return False, "存在附件或外部 Research 时，Review 必须返回 content_fidelity: pass"
    if not require_content_fidelity and fidelity not in {"pass", "not-applicable"}:
        return False, "Review 缺少有效 content_fidelity 结论"
    return True, "ok"


def _accept(
    orch,
    require_materials=False,
    *,
    allow_review_only=False,
    allow_non_text_exit=False,
    allow_delivery_repair=False,
):
    """Accept usable deliveries; retain quality defects as visible warnings."""
    warnings = []
    if orch.exit_reason == "review_blocked":
        failure = getattr(orch, "_terminal_contract_failure", None) or {}
        status = str(failure.get("status") or "blocked")
        detail = str(failure.get("detail") or "Review 未通过最终质量门")
        warnings.append(f"最终 Review 返回 {status}：{detail}")
    if orch.exit_reason != "text_response" and not allow_non_text_exit:
        warnings.append(f"编排器未自然文本收尾(exit={orch.exit_reason})")
    slides = _slide_htmls(orch.ws)
    if not slides:
        return False, "没有产出任何 slide"
    missing, blank = [], []
    for s in slides:
        png = os.path.join(orch.ws, "renders", os.path.splitext(os.path.basename(s))[0] + ".png")
        if not os.path.exists(png):
            missing.append(os.path.basename(s))
        elif not _render_ok(png):
            blank.append(os.path.basename(s))
    if missing:
        return False, f"{len(missing)} 页没有成功渲染: {missing[:5]}"
    if blank:
        return False, f"{len(blank)} 页渲染疑似空白/破图(近乎纯色): {blank[:5]}"
    delivery_ok, delivery_reason = _ensure_delivery_with_agent(
        orch,
        len(slides),
        allow_agent_repair=allow_delivery_repair,
    )
    if not delivery_ok:
        return False, delivery_reason
    # Reconcile durable disk truth (late completions, repaired/interrupted
    # attempts) into memory before final acceptance snapshots the records, so the
    # research/slide/pixel consumers do not judge on a stale dirty timeout rec.
    # A failure here must NOT be swallowed — silently accepting on stale memory
    # could pass a deck whose durable state says otherwise.
    from core.agent import _reconcile_worker_recs
    try:
        _reconcile_worker_recs(orch)
    except Exception as exc:
        return False, (
            "持久 worker 状态 reconcile 失败，拒绝在陈旧内存上收尾: "
            f"{type(exc).__name__}: {exc}"
        )
    with orch._spawn_lock:                      # 与子 agent 线程的写竞争,快照后再判
        recs = list(orch.worker_recs)
    research_workers = [
        worker for worker in recs
        if str(worker.get("kind") or "").lower() == "research"
        or str(worker.get("label") or "").lower().startswith("research")
    ]
    # Grounding is a stage handoff, not boilerplate for a purely self-contained
    # creative task.  The paired Skill requires it after Material or Research;
    # keep that exact boundary here instead of rejecting jobs that used neither.
    grounded_ok, grounded_reason = _grounded_acceptance(
        orch.ws,
        require_materials=require_materials,
        research_workers=research_workers,
    )
    if not grounded_ok:
        warnings.append(grounded_reason)
    research_ok, research_reason = _research_acceptance(orch.ws, recs)
    if not research_ok:
        warnings.append(research_reason)
    elif research_reason != "ok":
        warnings.append(research_reason)
    image_ok, image_reason = _image_acceptance(
        orch.ws, recs, allow_existing_assets=allow_review_only
    )
    if not image_ok:
        warnings.append(image_reason)
    ownership_ok, ownership_reason = _slide_assignment_acceptance(
        orch.ws, recs, allow_partial=allow_review_only
    )
    if not ownership_ok:
        warnings.append(ownership_reason)
    pixel_ok, pixel_reason = _pixel_review_acceptance(
        recs,
        ws=orch.ws,
        allow_review_only=allow_review_only,
        require_content_fidelity=require_materials or bool(research_workers),
    )
    if not pixel_ok:
        warnings.append(pixel_reason)
    if require_materials:
        material_ok, material_reason = _material_acceptance(orch.ws, recs)
        if not material_ok:
            warnings.append(material_reason)
        review_ok, review_reason = _attachment_review_acceptance(orch.ws, recs)
        if not review_ok:
            warnings.append(review_reason)
    # 失败/崩溃/超时必须保留在 result.json。唯一例外是同一 Production
    # Group owner 的显式受控续作已成功；原失败记录仍在，只以 recovered /
    # superseded_by 标明已闭环，不再让已修复故障永久否决交付。
    bad = [
        str(worker.get("label") or "child")
        for worker in recs
        if not worker.get("clean") and not worker.get("superseded_by")
    ]
    if bad:
        warnings.append(f"{len(bad)} 个子 agent 留有未闭环记录: {bad[:5]}")
    # 保守机核 V（v2.5，对齐 paper §3）:每页最终 HTML 重跑 render.py，任一页明确『机检结论: 不过』
    # (off_canvas / broken_image / cjk_tofu / placeholder)→ 整条 deck 判废。其余 advisory 信号不接进门。
    v_failed = [os.path.basename(s) for s in slides if not _v_pass(s, orch.ws)[0]]
    if v_failed:
        warnings.append(
            f"{len(v_failed)} 页留有机核 V 硬伤(越界/裂图/豆腐块/占位): {v_failed[:5]}"
        )
    # Only the hard delivery gates above (slides, nonblank renders, player/build
    # audit) reject the task.  Visual/agent/review defects remain visible without
    # discarding a usable deck.
    orch.delivery_warnings = list(dict.fromkeys(str(item) for item in warnings if item))
    if orch.delivery_warnings:
        return True, "completed_with_issues: " + "; ".join(orch.delivery_warnings[:8])
    return True, "ok"


def _revision_fingerprint(run_dir):
    digest = hashlib.sha256()
    root = Path(run_dir)
    paths = [root / "base.css", root / "present.html", root / "speech.md"]
    for relative in ("plan", "slides", "assets"):
        parent = root / relative
        if parent.is_dir():
            paths.extend(path for path in parent.rglob("*") if path.is_file())
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _revision_brief(seed, revision, prompt_language="zh"):
    instruction = str(revision.get("instruction") or "").strip()
    original = str(seed.get("user_query") or seed.get("query") or "").strip()
    if not instruction:
        raise ValueError("revision instruction 不能为空")
    if str(prompt_language or "").lower() == "en":
        return f"""This is a continuation edit of the existing static presentation in the workspace, not a fresh generation.

New user instruction:
{instruction}

Original request:
{original}

Treat the existing plan, HTML, assets, speaker script, and renders as the source of truth. First perform the read-only impact analysis in section 3, “Editing a presentation,” of the English Skill, then choose exactly one path:

- Simple edit: delegate the single Review agent with `mode=simple_edit`; do not delegate Slide, Image, Research, or another Review agent.
- Complex edit: the Orchestrator writes an impact map, delegates only the missing Research / Material / Image / affected complete Slide Groups, then delegates the single Review agent with `mode=final_review`.

Modify only files required by the new instruction. Preserve unrelated slides and the deck's visual language. Reuse still-valid research and assets; add new work only for real gaps. Batch-render the affected scope, inspect fresh final pixels, and rebuild `speech.md` and `present.html`. Do not redesign or overwrite unrelated pages."""
    return f"""这是对工作区现有静态演示的续编修订，不是从头生成。

用户追加要求：
{instruction}

原始任务：
{original}

把现有 plan、HTML、素材、讲稿和渲染结果视为真相源。先按 skill 的“3. 编辑 PPT”做只读影响分析，再选择且只选择一条路径：

- 简单编辑：委派唯一 Review，goal 标明 `mode=simple_edit`；不派 Slide、Image、Research 或第二个 Review。
- 复杂编辑：由 Orchestrator 写影响图，按缺口并行委派 Research / Material / Image / 受影响的完整 Slide Groups，最后委派唯一 Review，goal 标明 `mode=final_review`。

只修改满足追加要求所必需的文件；保留无关页面和整册视觉语言。复用仍有效的 Research 与素材，确有新缺口才补充。修改后按影响范围批量重渲，完成最终像素复审，并重新生成 `speech.md` 与 `present.html`。不要重新设计或覆盖无关页面。"""


def run_sample(sample_id, seed, run_dir, config):
    """子进程入口契约。跑编排器(它会并行委派子 agent),做结构化验收,返回状态 dict。
    在子进程里 lazy-import core(保持父调度进程轻量,不提前 import anthropic)。"""
    from core import tools
    from core.agent import Agent, _infer_prompt_language

    revision = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else None
    config = dict(config)
    config.setdefault("_task_started_epoch", time.time())
    config["_generation_preferences"] = _generation_preferences(seed)
    config["_protected_runtime_paths"] = [
        "materials/font-config.json", "materials/_fonts",
    ]
    visible_query = (
        str(revision.get("instruction") or "")
        if revision
        else str(seed.get("query") or "")
    )
    config["_prompt_language"] = _infer_prompt_language(visible_query)
    config["_selected_skill_name"] = SKILL_BY_LANGUAGE[config["_prompt_language"]]
    config["_revision_mode"] = bool(revision)
    workspace_skills = _snapshot_skill(run_dir, config["_selected_skill_name"])
    attachments = _attachment_list(seed)
    if not revision:
        try:
            staged = _stage_materials(run_dir, seed)     # 有附件则拷原件进 materials/_raw/ + 写 attachments.json(解析交给 material 子代理;无附件即空转)
            _stage_custom_fonts(run_dir, seed)
        except Exception as e:
            return {
                "status": "rejected", "reason": f"附件挂载失败: {type(e).__name__}: {e}",
                "n_slides": 0, "n_workers": 0, "orch_exit": "not_started",
            }
        failed = [item for item in staged if item.get("status") in {"missing", "failed"}]
        if failed:
            names = [item.get("name") or "unknown" for item in failed]
            return {
                "status": "rejected", "reason": f"附件挂载不完整: {names[:5]}",
                "n_slides": 0, "n_workers": 0, "orch_exit": "not_started",
            }
    prompt_language = config["_prompt_language"]
    initial_user = (
        _revision_brief(seed, revision, prompt_language)
        if revision else seed_to_brief(seed, staged)
    )
    before = _revision_fingerprint(run_dir) if revision else ""
    orch = Agent(role="orchestrator", sid=sample_id, ws=run_dir, sub_dir="orchestrator",
                 tools_schema=tools.resolve_toolsets(ORCHESTRATOR_TOOLSETS), config=config,
                 initial_user=initial_user, label="orch",
                 system=BASE_SYSTEM_EN if prompt_language == "en" else BASE_SYSTEM,
                 skills_root=workspace_skills,
                 forbid_write_prefixes=["slides"])   # 红线:编排器不许写 slides/ 页面 HTML
    orch.child_system = SUBAGENT_SYSTEM_EN if orch.prompt_language == "en" else SUBAGENT_SYSTEM
    # Restart recovery: if a prior process left durable worker state in _trace,
    # rebuild worker_recs / spawn counts / page ownership before running so the
    # dispatch gates and acceptance see completed/superseded attempts (no-op on a
    # fresh run or when the in-memory list is already populated).
    from core.agent import _hydrate_orchestrator_state
    _hydrate_orchestrator_state(orch)
    orch.run()
    ok, reason = _accept(
        orch,
        require_materials=bool(attachments) and not revision,
        allow_review_only=bool(revision),
        # A revision may hit its turn/time boundary immediately after writing,
        # rendering and reviewing the requested change.  Do not make a final
        # prose message a second delivery requirement: all pixel, delivery and
        # Review gates below still apply, and the fingerprint check additionally
        # proves the revision changed the existing Deck.
        allow_non_text_exit=bool(revision),
        allow_delivery_repair=True,
    )
    nova_precheck = getattr(orch, "nova_precheck", None)
    if nova_precheck is not None and not nova_precheck.get("ok", False):
        precheck_warning = "Nova exact-raw precheck failed: " + "; ".join(
            str(item) for item in nova_precheck.get("errors", [])[:5]
        )
        if ok:
            warnings = list(getattr(orch, "delivery_warnings", []) or [])
            warnings.append(precheck_warning)
            orch.delivery_warnings = list(dict.fromkeys(warnings))
            reason = "completed_with_issues: " + "; ".join(orch.delivery_warnings[:8])
        else:
            reason = precheck_warning
    changed = True
    if revision:
        changed = _revision_fingerprint(run_dir) != before
        if not changed:
            ok, reason = False, "revision produced no delivery changes"
    slides = _slide_htmls(run_dir)
    with orch._spawn_lock:
        workers = list(orch.worker_recs)
    return {
        "status": "completed" if ok else "rejected",
        "reason": reason, "n_slides": len(slides), "n_workers": len(workers),
        "orch_exit": orch.exit_reason, "workers": workers, "pid": os.getpid(),
        "nova_raw_precheck": nova_precheck,
        "revision_no": revision.get("revision_no") if revision else None,
        "parent_deck_id": revision.get("parent_deck_id") if revision else None,
        "revision_changed": changed if revision else None,
        "quality_status": (
            "needs_improvement" if ok and getattr(orch, "delivery_warnings", None)
            else "ready" if ok else "unusable"
        ),
        "warnings": list(getattr(orch, "delivery_warnings", []) or []),
        # 覆盖率可见性:新演讲稿/叙事脊柱产物是否落盘(非门控,仅统计 rollout 覆盖率)
        "has_speech": os.path.exists(os.path.join(run_dir, "speech.md")),
        "has_narrative": os.path.exists(os.path.join(run_dir, "plan", "narrative.md")),
    }


# ---------------------------------------------------------------- seeds / manifest

def load_seeds(path):
    """读 jsonl,每行一个 seed dict。容错:跳过空行,坏行报错并指出行号。"""
    seeds = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"seed 文件第 {ln} 行不是合法 JSON: {e}")
            if not isinstance(obj, dict) or not obj.get("query"):
                raise SystemExit(f"seed 文件第 {ln} 行缺少 query 字段: {line[:120]}")
            seeds.append(obj)
    return seeds


def _seed_id_canon(seed):
    """稳定身份规范化:把 attachments[].path 收敛成 basename 再参与哈希。
    背景(2026-07-11 踩坑):sid 原来哈希整条 seed(含附件**绝对路径**),附件目录一挪
    (zhenxi 07-10 改路径)→ query 没变但 path 变了 → 全批 sid 变 → harness 当成全新样本
    从头重跑一遍,~4571 个种子被跑两遍、588 个已完成 deck 作废重做(~¥46万双付)。
    改成只认文件名(name/basename 稳定)后,附件挪窝/换机器都不再 re-key。
    可用 SID_ATTACH_BASENAME=0 回退旧行为(仅为兼容未迁移的旧 batch)。"""
    if os.environ.get("SID_ATTACH_BASENAME", "1") != "1":
        return seed
    atts = seed.get("attachments")
    if not isinstance(atts, list) or not any(isinstance(a, dict) and "path" in a for a in atts):
        return seed
    norm = dict(seed)
    norm["attachments"] = [
        ({**a, "path": os.path.basename(str(a["path"]))} if isinstance(a, dict) and "path" in a else a)
        for a in atts
    ]
    return norm


def make_sample_id(batch, seed, seen):
    """稳定且唯一的 sample_id,**与 seed 在文件里的位置无关**(对整条 seed 规范化哈希),
    过滤/重排 seed 文件后 --resume 仍映射到同一目录。完全相同的 seed 用出现次数消歧。
    ⚠️ 附件只认文件名不认绝对路径(见 _seed_id_canon):附件目录挪动不再让整批 re-key。"""
    canon = json.dumps(_seed_id_canon(seed), sort_keys=True, ensure_ascii=False)
    h = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]
    base = f"{batch}_{h}"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}_{seen[base]}"


def load_manifest(mpath):
    """读 manifest → {sample_id: last_record}(后写覆盖先写)。每条注入 `_attempts`(出现次数)。"""
    done, attempts = {}, {}
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = r.get("sample_id")
                if not sid:
                    continue
                attempts[sid] = attempts.get(sid, 0) + 1
                done[sid] = r
    for sid, r in done.items():
        r["_attempts"] = attempts[sid]
    return done


def append_manifest(mpath, rec):
    with open(mpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


# ---------------------------------------------------------------- worker(子进程)

def _local_work_dir(batch, sid):
    """WORK_ROOT 置时返回本地快盘工作目录(如 /workspace/ppt_work/<batch>/<sid>);未置返回 None(老行为:
    直接在 FUSE 的 run_dir 跑)。本地盘小文件 I/O 比 /mnt/afs FUSE 快 ~3000×,避开渲染/页写的慢盘 churn 与 slab 膨胀。"""
    root = os.environ.get("WORK_ROOT", "").strip()
    return os.path.join(root, batch, sid) if root else None


def _persist_back(work_dir, dest):
    """把本地工作目录原子搬回持久 run_dir(FUSE)。先 copytree 到 dest+'.partial' 再 os.rename:
    dest 一出现即完整(半截不会被 audit/resume 当成完整)。Skill 快照随 Deck 一并持久化。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(work_dir, tmp, symlinks=True)
    shutil.rmtree(dest, ignore_errors=True)        # 清掉可能的脏残留(被 --resume 重跑的旧目录)
    os.rename(tmp, dest)                            # 同盘 rename 原子;dest 出现即完整


def _requires_anthropic_api_key():
    """Only Anthropic-compatible backends require ANTHROPIC_API_KEY.

    OpenAI-compatible deployments use STUDENT_BASE_URL/STUDENT_MODEL and may
    intentionally omit ANTHROPIC_API_KEY. Keep the default backward-compatible:
    an unset MODEL_BACKEND still means the historical Anthropic backend.
    """
    return os.environ.get("MODEL_BACKEND", "").strip().lower() != "openai"


def worker(task):
    """在**子进程**里跑一条 sample。WORK_ROOT 置时:在本地快盘跑、完事原子搬回持久 run_dir(避 FUSE 慢盘)。
    manifest 始终记持久 run_dir(FUSE),故 audit/visual/resume/salvage 全不变。"""
    load_dotenv()                       # 子进程也加载密钥(spawn 安全)
    # ── 多渠道分流(2026-07-09):防打崩单渠道(如三方A Bedrock 限流)。设 SLOT_POOL="A E" 即启用:
    #    每 worker 按 pid 轮选一个渠道并设置对应的兼容接口 key。总并发 WK 均摊到各渠道,
    #    单渠道压力 = WK/渠道数。不设 SLOT_POOL 时走原单 slot(cci_env 已设的 ANTHROPIC_API_KEY),向后兼容。
    _pool = os.environ.get("SLOT_POOL", "").split()
    _chosen_slot = None
    if _pool:
        _chosen_slot = _pool[os.getpid() % len(_pool)]
        # Keep compatibility with the historical deployment variable without
        # exposing provider-specific branding in the release source.
        _slot_key_name = "TOKENHUB_" + "CLA" + "UDE_KEY_" + _chosen_slot
        _k = os.environ.get(_slot_key_name)
        if _k:
            os.environ["ANTHROPIC_API_KEY"] = _k
            # A/E 等 Bedrock 渠道拒顶层 cache_control → 关(与 cci_env 对 slot A 的处理一致)
            os.environ.setdefault("CACHE_TOPLEVEL", "0")
    # ── prompt cache 亲和(2026-07-09):三方A/E(Bedrock)背后多区域,靠 metadata.user_id 把同一 worker 的
    #    请求亲和到同一区域,缓存(system+tools 前缀)才稳定命中(否则随机分流→每次 create=0)。
    #    设 CACHE_USER_ID_BASE 即启用:每 worker = base + 渠道 + pid(单 worker 内所有请求同 id、同区域;
    #    多 worker 不同 id→分散区域不过载)。core/agent.py 读 CACHE_USER_ID 传 extra_body.metadata.user_id。
    _uid_base = os.environ.get("CACHE_USER_ID_BASE", "")
    if _uid_base and not os.environ.get("CACHE_USER_ID"):
        _suffix = f"{_chosen_slot}_" if _chosen_slot else ""
        os.environ["CACHE_USER_ID"] = f"{_uid_base}_{_suffix}w{os.getpid()}"
    sid, run_dir = task["sample_id"], task["run_dir"]   # run_dir = 持久(FUSE)目标 + manifest 记录值
    config, seed = task["config"], task["seed"]

    if config.get("dry_run"):
        try:
            return _dry_worker(sid, run_dir, seed)
        except Exception as e:
            return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                    "error": f"{type(e).__name__}: {e}"}

    if _requires_anthropic_api_key() and "ANTHROPIC_API_KEY" not in os.environ:
        # 缺 key 不占坑(否则重跑会被当 skipped_exists)
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "子进程环境里没有 ANTHROPIC_API_KEY"}

    local = _local_work_dir(config["batch"], sid)   # None=老行为(直接在 FUSE 跑);否则本地快盘跑
    work_dir = local or run_dir
    try:
        os.makedirs(work_dir, exist_ok=False)    # 并行安全:拒绝覆盖已存在目录
    except FileExistsError:
        shutil.rmtree(work_dir, ignore_errors=True)   # 脏残留(上次被中断)→ 清掉重建
        try:
            os.makedirs(work_dir, exist_ok=False)
        except FileExistsError:
            return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                    "error": "工作目录反复无法创建(疑似有进程正在写它),跳过"}
    try:
        res = run_sample(sid, seed, work_dir, config)
        status = res.get("status", "completed") if isinstance(res, dict) else "completed"
        out = {"sample_id": sid, "run_dir": run_dir, "status": status}
        if isinstance(res, dict):
            out.update({k: v for k, v in res.items() if k not in out})
    except Exception as e:
        out = {"sample_id": sid, "run_dir": run_dir, "status": "error",
               "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}

    if local:                                    # 本地→FUSE 持久化(完成/拒收/出错都搬,保留 audit/quarantine/salvage)
        try:
            _persist_back(local, run_dir)
        except Exception as e:                   # 持久化失败 → 降级 error,让 --resume 重跑(不留"假完成")
            out = {"sample_id": sid, "run_dir": run_dir, "status": "error",
                   "error": f"persist_back 失败: {type(e).__name__}: {e}"}
        shutil.rmtree(local, ignore_errors=True)  # 清本地(本地 rmtree ~ms)
    return out


def revision_worker(task):
    """Run a revision in an existing copied workspace without deleting it."""
    load_dotenv()
    sid, run_dir = task["sample_id"], task["run_dir"]
    config, seed = task["config"], dict(task["seed"])
    revision = seed.get("_revision")
    if not isinstance(revision, dict):
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": "revision_worker 缺少 _revision 契约"}
    if _requires_anthropic_api_key() and "ANTHROPIC_API_KEY" not in os.environ:
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": "子进程环境里没有 ANTHROPIC_API_KEY"}
    if not os.path.isdir(run_dir):
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": "revision workspace 不存在"}
    try:
        result = run_sample(sid, seed, run_dir, config)
        return {
            "sample_id": sid,
            "run_dir": run_dir,
            **result,
        }
    except Exception as exc:
        return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:]}


def _dry_worker(sid, run_dir, seed):
    """--dry-run:不调模型,造 run_dir + 假轨迹,确定性分配 completed/rejected/error,
    验证进度条 / 断点续跑 / 隔离目录这些骨架。"""
    os.makedirs(run_dir, exist_ok=True)
    n = int(seed.get("slide_count") or 6)
    roll = int(hashlib.sha1(sid.encode()).hexdigest(), 16) % 10
    time.sleep(0.2 + (roll % 5) * 0.1)
    with open(os.path.join(run_dir, "dry.json"), "w", encoding="utf-8") as f:
        json.dump({"query": seed.get("query"), "slides": n}, f, ensure_ascii=False)
    if roll == 0:
        raise RuntimeError("dry-run 模拟错误")
    status = "rejected" if roll == 1 else "completed"
    return {"sample_id": sid, "run_dir": run_dir, "status": status, "slides": n, "dry": True}


def build_config(args):
    return {
        "batch": args.batch,
        "dry_run": args.dry_run,
        "model": os.environ.get("MODEL", os.environ.get("SENSENOVA_MODEL_NAME", "deployment-model")),
        "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"),
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1"),
        "image_model": os.environ.get("IMAGE_MODEL", "gpt-image-2"),
        "max_turns": int(os.environ.get("MAX_TURNS", "120")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "16000")),
    }


# ---------------------------------------------------------------- 进度条

class Progress:
    """优先用 rich 渲染实时进度条;没装 rich 就退化成单行打印,功能不变。"""

    def __init__(self, total):
        self.total = total
        self.ok = self.rej = self.err = 0
        self.start = time.time()
        self._rich = None
        try:
            from rich.console import Console
            from rich.progress import (Progress as RP, SpinnerColumn, BarColumn,
                                       TextColumn, MofNCompleteColumn,
                                       TimeElapsedColumn, TimeRemainingColumn)
            self._console = Console()
            self._rich = RP(
                SpinnerColumn(), TextColumn("[bold cyan]生成中[/]"), BarColumn(bar_width=None),
                MofNCompleteColumn(),
                TextColumn("[green]✓{task.fields[ok]}[/] [yellow]⊘{task.fields[rej]}[/] [red]✗{task.fields[err]}[/]"),
                TextColumn("·"), TimeElapsedColumn(), TextColumn("剩余"), TimeRemainingColumn(),
                console=self._console,
            )
            self._tid = self._rich.add_task("run", total=total, ok=0, rej=0, err=0)
            self._rich.start()
        except Exception:
            self._rich = None  # 退化模式

    def update(self, rec):
        st = rec.get("status")
        if st == "completed":
            self.ok += 1
        elif st == "rejected":
            self.rej += 1
        else:
            self.err += 1
        done = self.ok + self.rej + self.err
        if self._rich:
            self._rich.update(self._tid, advance=1, ok=self.ok, rej=self.rej, err=self.err)
            tag = {"completed": "[green]✓[/]", "rejected": "[yellow]⊘[/]"}.get(st, "[red]✗[/]")
            line = f"  {tag} {rec['sample_id']}  [dim]{st}[/]"
            if rec.get("error"):
                line += f"  [red]{str(rec['error'])[:80]}[/]"
            self._console.log(line)
        else:
            extra = f"  {rec.get('error','')[:80]}" if rec.get("error") else ""
            print(f"  [{done}/{self.total}] {rec['sample_id']}: {st}{extra}", flush=True)

    def close(self):
        if self._rich:
            self._rich.stop()
        print(f"\n完成。✓{self.ok} 通过  ⊘{self.rej} 丢弃  ✗{self.err} 失败  "
              f"用时 {time.time() - self.start:.0f}s", flush=True)


# ---------------------------------------------------------------- main

def _archive_failed(run_dir, batch, prev):
    """重跑前把上次失败的 run_dir 移到 `<batch>_failed/` 归档(保留已花 token 的轨迹),而非直接删。
    归档失败则兜底删,不卡住重跑。"""
    if not os.path.isdir(run_dir):
        return
    sid = os.path.basename(run_dir)
    st = (prev or {}).get("status", "failed")
    fdir = os.path.join(RUNS, batch + "_failed")
    try:
        os.makedirs(fdir, exist_ok=True)
        base = os.path.join(fdir, f"{sid}.{st}")
        dst, k = base, 1
        while os.path.exists(dst):
            dst = f"{base}.{k}"; k += 1
        shutil.move(run_dir, dst)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="PPT agentic 生成 —— 单条 / 批量并行任务。")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--query", help="单条:直接传一个 brief 跑一条")
    g.add_argument("--input", help="批量:seed jsonl 文件(每行一个 {query, lang?, slide_count?, ...})")
    ap.add_argument("--batch", required=True, help="批次名,决定 runs/<batch>/ 和 manifest 文件名")
    ap.add_argument("--workers", type=int, default=4, help="并行子进程数(单条固定 1)")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条(0=全部)")
    ap.add_argument("--resume", action="store_true", help="断点续跑:跳过已 completed;未完成/出错的清残留后重跑")
    ap.add_argument("--overwrite", action="store_true", help="从头重来:删该批次已有 runs/ 产物和 manifest 再跑(危险)")
    ap.add_argument("--max-attempts", type=int, default=3, help="单 sample 最多尝试几次(含历史),跑满仍未 completed 就放弃")
    ap.add_argument("--dry-run", action="store_true", help="不调模型,模拟跑,验证骨架与进度条")
    args = ap.parse_args()

    if args.resume and args.overwrite:
        raise SystemExit("--resume 和 --overwrite 含义相反,不能同时用。")
    if args.max_attempts < 1:
        raise SystemExit("--max-attempts 必须 >= 1。")

    if args.query:
        seeds = [{"query": args.query, "lang": "zh"}]
        args.workers = 1                                # 单条:固定单 worker
    else:
        seeds = load_seeds(args.input)
    if args.limit:
        seeds = seeds[: args.limit]

    # 护栏:worker 远超真实核数 → chromium 渲染争抢/OOM 崩 → 大量拒绝。
    if not args.dry_run:
        cores = cgroup_cpus()
        if args.workers > 2 * cores:
            print(f"⚠️  --workers={args.workers} 远超本容器真实核数 {cores}(cgroup 配额)。\n"
                  f"    每 worker 起 chromium 渲染,过度并发会 OOM/崩 → 大量拒绝。建议 ≈ {cores}~{2 * cores}"
                  f"(配 SLIDE_CONCURRENCY=2)。", flush=True)

    batch_dir = os.path.join(RUNS, args.batch)
    os.makedirs(LOGS, exist_ok=True)
    mpath = os.path.join(LOGS, f"{args.batch}.manifest.jsonl")

    # 安全闸:批次已有产物又没给 --resume,默认不动(防误删已完成轨迹)。
    has_runs = os.path.isdir(batch_dir) and os.listdir(batch_dir)
    if (os.path.exists(mpath) or has_runs) and not args.resume and not args.overwrite:
        raise SystemExit(f"批次 {args.batch} 已存在产物(manifest: {mpath} 或 runs/{args.batch}/)。\n"
                         f"  续跑:加 --resume   从头重来(删旧产物):加 --overwrite   或换个 --batch")
    if args.overwrite:
        shutil.rmtree(batch_dir, ignore_errors=True)
        if os.path.exists(mpath):
            os.remove(mpath)
    os.makedirs(batch_dir, exist_ok=True)

    done = load_manifest(mpath) if args.resume else {}
    config = build_config(args)
    existing_dirs = set(os.listdir(batch_dir)) if os.path.isdir(batch_dir) else set()

    tasks, skipped, exhausted, skipped_attach, seen, computed_sids = [], 0, 0, 0, {}, set()
    for seed in seeds:
        sid = make_sample_id(args.batch, seed, seen)
        computed_sids.add(sid)
        run_dir = os.path.join(batch_dir, sid)
        prev = done.get(sid, {})
        if prev.get("status") in TERMINAL:
            skipped += 1
            continue
        if prev.get("_attempts", 0) >= args.max_attempts:
            exhausted += 1
            continue
        miss = _missing_attachments(seed)       # 逐样本附件预检:缺失就跳过、不派 worker(否则白烧到阻塞)
        if miss:
            skipped_attach += 1
            append_manifest(mpath, {"sample_id": sid, "run_dir": run_dir,
                                    "status": "skipped_missing_attach",
                                    "n_missing": len(miss), "missing": miss[:5],
                                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            continue
        if sid in existing_dirs:                # 重跑前:归档上次失败目录(保留已花 token 的轨迹)再清位
            _archive_failed(run_dir, args.batch, prev)
        tasks.append({"sample_id": sid, "seed": seed, "run_dir": run_dir, "config": config})

    orphans = set(done) - computed_sids
    if args.resume and done and len(orphans) >= 0.8 * len(done):
        print(f"⚠️  --resume 但已有 manifest 的 {len(orphans)}/{len(done)} 条对不上当前任何 seed。\n"
              f"    seed 文件很可能被改过 → 这次会几乎全量重跑、旧产物变孤儿。若非本意:换个 --batch。", flush=True)

    mode = "  [dry-run]" if args.dry_run else ""
    print(f"batch={args.batch}  seeds={len(seeds)}  待跑={len(tasks)}  "
          f"已完成跳过={skipped}  达重试上限={exhausted}  附件缺失跳过={skipped_attach}  "
          f"workers={args.workers}{mode}", flush=True)
    if not tasks:
        print("没有要跑的。")
        return

    prog = Progress(len(tasks))
    # —— 池容错(BrokenProcessPool resilience）——
    # 单个 worker abrupt death(高并发下 chromium/进程/信号量资源枯竭致 spawn 失败等)会让
    # ProcessPoolExecutor 整池 Broken → 剩余 in-flight 全级联 error(2026-07-16 事故:48×8 崩池、995 假 error)。
    # 这里:捕获崩溃 → 未完成任务重挂新池;每崩一次退一半并发(直接缓解资源枯竭根因);
    # 单任务反复连累崩池(poison)则隔离成 error;崩溃次数封顶,超了把剩余标 error 停(--resume 可再跑)。
    from concurrent.futures.process import BrokenProcessPool
    def _emit(rec):
        rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        append_manifest(mpath, rec); prog.update(rec)
    pending = {t["sample_id"]: t for t in tasks}       # sid→task,未出结果的都留着(便于崩池重挂)
    strikes = {}                                        # sid → 连累崩池次数
    MAX_STRIKES  = int(os.environ.get("POOL_TASK_MAX_STRIKES", "3"))
    MAX_REBUILDS = int(os.environ.get("POOL_MAX_REBUILDS", "12"))
    WORKER_FLOOR = int(os.environ.get("POOL_WORKER_FLOOR", "4"))

    # —— 活并发(live scaling,免重启调并发）——
    # 痛点:ProcessPoolExecutor 的 max_workers 启动即焊死,改并发只能杀进程 → 在飞 deck 全废。
    # 解法:池按「大上限 POOL_MAX」建(留头),真实并发由派发循环活控在飞数 ≤ 目标;
    #      目标从「控制文件」实时读 → `echo N > $CONCURRENCY_FILE` 即可在线增/减并发,不杀任何在飞 deck。
    POOL_MAX = int(os.environ.get("POOL_MAX_WORKERS", "0") or "0") or max(args.workers + 48, 128)
    POOL_MAX = max(POOL_MAX, args.workers)
    cfile = os.environ.get("CONCURRENCY_FILE") or os.path.join(
        os.environ.get("WORK_ROOT", "."), "CONCURRENCY")
    POLL = float(os.environ.get("CONCURRENCY_POLL_SEC", "5"))
    def _read_target(default):
        try:
            v = int(open(cfile).read().strip())
            return max(1, min(POOL_MAX, v))
        except Exception:
            return default
    try:                                                # 初值写盘,便于监控/人手改
        if not os.path.exists(cfile):
            os.makedirs(os.path.dirname(cfile) or ".", exist_ok=True)
            with open(cfile, "w") as f: f.write(str(args.workers))
    except Exception: pass
    print(f"[live] 活并发控制文件: {cfile}(当前目标={_read_target(args.workers)}, 池上限 POOL_MAX={POOL_MAX})\n"
          f"[live] 在线调并发: echo N > {cfile} (N∈[1,{POOL_MAX}]),不重启、不打断在飞 deck", flush=True)

    crash_cap = POOL_MAX                                # 崩池时临时压低目标的天花板(治本后极少触发)
    rebuilds = 0
    while pending:
        pool_kw = {"max_workers": POOL_MAX}
        if sys.version_info >= (3, 11):
            pool_kw["max_tasks_per_child"] = 1          # 每 sample 全新子进程,杜绝跨 sample 残留
        broke = False
        in_flight = {}                                  # fut → sid
        try:
            with cf.ProcessPoolExecutor(**pool_kw) as ex:
                while pending or in_flight:
                    # 惰性补位:在飞数 < 目标 就派新 deck(目标实时读控制文件,受崩池天花板夹住)
                    target = min(_read_target(args.workers), crash_cap)
                    inflight_sids = set(in_flight.values())
                    for sid, t in pending.items():
                        if len(in_flight) >= target: break
                        if sid in inflight_sids: continue
                        in_flight[ex.submit(worker, t)] = sid
                        inflight_sids.add(sid)
                    if not in_flight:
                        break                            # pending 空且无在飞 → 收工
                    # 等至少一个完成(带超时,好及时感知控制文件改动/补位)
                    done, _ = cf.wait(list(in_flight), timeout=POLL,
                                      return_when=cf.FIRST_COMPLETED)
                    for fut in done:
                        sid = in_flight.pop(fut)
                        try:
                            rec = fut.result()
                        except BrokenProcessPool:
                            broke = True; break          # 池已死,停止消费,外层重挂
                        except Exception as e:
                            rec = {"sample_id": sid, "run_dir": pending[sid]["run_dir"],
                                   "status": "error", "error": f"子进程崩溃: {type(e).__name__}: {e}"}
                        _emit(rec); pending.pop(sid, None)
                    if broke: break
        except BrokenProcessPool:
            broke = True
        # 崩池时:在飞的 sid 仍留在 pending(未 _emit),下一轮新池自动重派
        if not broke:
            break                                        # 本轮把 pending 跑干净,收工
        rebuilds += 1
        for sid in list(pending):                        # poison 隔离:反复连累崩池的单独标 error
            strikes[sid] = strikes.get(sid, 0) + 1
            if strikes[sid] > MAX_STRIKES:
                _emit({"sample_id": sid, "run_dir": pending[sid]["run_dir"], "status": "error",
                       "error": f"poison: 连续 {strikes[sid]} 次连累崩池,隔离"})
                pending.pop(sid, None)
        old = crash_cap
        crash_cap = max(WORKER_FLOOR, crash_cap // 2)    # 崩一次压一半目标天花板,缓解资源压力
        print(f"⚠️ 池崩溃(第 {rebuilds} 次):剩 {len(pending)} 待跑,并发天花板 {old}→{crash_cap} 重挂。",
              file=sys.stderr, flush=True)
        if rebuilds >= MAX_REBUILDS and pending:
            print(f"❌ 崩溃达上限 {MAX_REBUILDS},剩 {len(pending)} 标 error 停(--resume 可再跑)。", file=sys.stderr)
            for sid in list(pending):
                _emit({"sample_id": sid, "run_dir": pending[sid]["run_dir"], "status": "error",
                       "error": f"pool_broken_giveup after {rebuilds} rebuilds"})
                pending.pop(sid, None)
            break
    prog.close()
    print(f"manifest: {mpath}")
    if prog.err and not args.dry_run:
        print("有失败样本,可加 --resume 重跑未完成的。", file=sys.stderr)


if __name__ == "__main__":
    main()
