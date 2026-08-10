"""Async job dispatcher: queued decks -> engine subprocesses.

By default every queued deck starts immediately.  Operators can still set a
positive STUDIO_MAX_PER_MODEL to opt into a bounded per-model worker pool.
Deck status is queued -> running -> completed | failed, persisted in SQLite.

Restart safety: engine subprocesses survive a studio restart (they are separate
OS processes). On startup, a deck stuck in 'running' is ADOPTED if its engine
process is still alive (a watcher polls until it finishes, then finalizes) —
NEVER re-enqueued while alive, which would spawn a second engine fighting over
the same run_dir. Only decks with no live process and no result get requeued.
"""
import asyncio
import glob
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import custom_models, engine, service_config, trace
from .db import connect

# 0/负数表示不限制顶层 Deck 并发；正数才启用「每个模型各一个池」。
MAX_PER_MODEL = int(os.environ.get("STUDIO_MAX_PER_MODEL",
                                   os.environ.get("STUDIO_MAX_CONCURRENT", "0")))

_queues: dict = {}         # model_key -> asyncio.Queue(各模型独立队列)
_running: dict = {}        # deck_id -> asyncio subprocess
_workers: list = []
_scheduled: set = set()    # unbounded 模式下防止同一 Deck 被重复派发
_loop = None               # sync FastAPI endpoint 通过它安全投递到 ASGI loop
_user_canceled: set = set()   # 用户主动取消的 deck:收尾时显示「已取消」而非报错


def _set_status(deck_id, status, **fields):
    con = connect()
    try:
        cols = ["status = ?"]
        vals = [status]
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            vals.append(v)
        vals.append(deck_id)
        con.execute(f"UPDATE decks SET {', '.join(cols)} WHERE id = ?", vals)
        con.commit()
    finally:
        con.close()


def _load_deck(deck_id):
    con = connect()
    try:
        return con.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    finally:
        con.close()


def _matching_engine_pids(deck_id) -> set[int]:
    """Return matching inference-worker PIDs without assuming Linux ``/proc``."""
    needle = os.path.join("jobs", f"{deck_id}.json")
    matches: set[int] = set()
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            process_entries = tuple(proc_root.iterdir())
        except OSError:
            process_entries = ()
        for entry in process_entries:
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().decode("utf-8", "ignore")
            except OSError:
                continue
            if "serve_one.py" in cmd and needle in cmd:
                matches.add(int(entry.name))
        return matches

    command: list[str] | None = None
    if os.name == "nt":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            command = [
                powershell, "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "\"$($_.ProcessId)`t$($_.CommandLine)\" }",
            ]
    else:
        ps = shutil.which("ps")
        if ps:
            command = [ps, "-axo", "pid=,command="]

    if command:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return matches
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "serve_one.py" not in line or needle not in line:
                    continue
                found = re.match(r"\s*(\d+)(?:\s+|\t)", line)
                if found:
                    matches.add(int(found.group(1)))
    return matches


def _engine_alive(deck_id) -> bool:
    """Return whether this deck's inference worker is alive."""
    return bool(_matching_engine_pids(deck_id))


def _revision_seed(row) -> dict | None:
    try:
        seed = json.loads(row["seed_json"] or "{}")
    except (TypeError, ValueError):
        return None
    revision = seed.get("_revision")
    return revision if isinstance(revision, dict) else None


def _prepare_revision_workspace(row, revision: dict) -> None:
    """Prepare either an in-place revision or a legacy immutable workspace."""
    parent_id = int(revision.get("parent_deck_id") or 0)
    target = Path(row["run_dir"] or "")
    if revision.get("in_place"):
        if parent_id != int(row["id"]):
            raise RuntimeError("原地修订的 deck 来源与目标不一致")
        try:
            seed = json.loads(row["seed_json"] or "{}")
        except (TypeError, ValueError):
            seed = {}
        entry = "deck.html" if seed.get("ppt_output") == "dynamic_html" else "present.html"
        if not (target / entry).is_file():
            raise RuntimeError(f"当前成稿缺少 {entry}，不能原地续编")

        # Keep only lightweight run metadata for diagnosis.  The presentation,
        # renders and output path stay exactly where they are and are edited by
        # revision_worker directly; no second Deck/workspace is created.
        revision_no = int(revision.get("revision_no") or 1)
        archive = target / "_trace" / "revisions" / f"revision_{revision_no:03d}"
        archive.mkdir(parents=True, exist_ok=True)
        result = target / "result.json"
        if result.is_file():
            candidates = [archive / "result-before.json"]
            candidates.extend(archive / f"result-attempt-{n:02d}.json" for n in range(1, 100))
            destination = next(path for path in candidates if not path.exists())
            result.replace(destination)
        previous_log = engine.log_path(int(row["id"]))
        if previous_log.is_file():
            artifact_snapshot = archive / "specialist-artifacts.json"
            if not artifact_snapshot.exists():
                try:
                    previous_feed = trace.livefeed(str(previous_log), run_dir=str(target))
                    artifact_payload = trace.scoped_specialist_artifacts(
                        str(target), previous_feed.get("agents", {}).keys()
                    )
                    temporary = artifact_snapshot.with_suffix(".json.tmp")
                    temporary.write_text(
                        json.dumps(artifact_payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    temporary.replace(artifact_snapshot)
                except (OSError, TypeError, ValueError):
                    # Archiving diagnostics must never prevent an otherwise
                    # valid in-place revision from starting.
                    pass
            log_candidates = [archive / "job-before.log"]
            log_candidates.extend(archive / f"job-attempt-{n:02d}.log" for n in range(1, 100))
            destination = next(path for path in log_candidates if not path.exists())
            shutil.copy2(previous_log, destination)
        (archive / "request.json").write_text(json.dumps({
            "deck_id": int(row["id"]),
            "revision_no": revision_no,
            "instruction": str(revision.get("instruction") or ""),
            "in_place": True,
            "created_at": int(time.time()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    parent = _load_deck(parent_id)
    if not parent or parent["status"] != "completed":
        raise RuntimeError("父版本尚未完成，不能创建修订工作区")
    source = Path(parent["run_dir"] or "")
    if not (source / "present.html").is_file():
        raise RuntimeError("父版本缺少 present.html，不能继续修改")

    marker = target / "_trace" / "revision-parent.json"
    if target.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise RuntimeError("修订工作区已存在但来源标记无效")
        if int(previous.get("parent_deck_id") or 0) != parent_id:
            raise RuntimeError("修订工作区与父版本不匹配")
        return

    def ignore(_directory, names):
        skipped = {"_trace", "result.json"}
        skipped.update(name for name in names if name.startswith("deck_") and name.endswith(".pptx"))
        return skipped.intersection(names)

    target.parent.mkdir(parents=True, exist_ok=True)
    # Revisions are immutable snapshots: materialize any parent symlink instead
    # of coupling the child to a path that may later change or disappear.
    shutil.copytree(source, target, symlinks=False, ignore=ignore)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "parent_deck_id": parent_id,
        "revision_no": int(revision.get("revision_no") or 1),
        "instruction": str(revision.get("instruction") or ""),
        "source_run_dir": str(source),
        "created_at": int(time.time()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _settle_waiting_revisions(parent_deck_id: int, succeeded: bool) -> None:
    """Release waiting children after a parent completes, or fail them with it."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT id FROM decks WHERE parent_deck_id = ? AND status = 'waiting' "
            "ORDER BY id",
            (parent_deck_id,),
        ).fetchall()
        if not rows:
            return
        if succeeded:
            con.executemany(
                "UPDATE decks SET status='queued', error=NULL WHERE id = ?",
                [(row["id"],) for row in rows],
            )
        else:
            con.executemany(
                "UPDATE decks SET status='failed', error=?, finished_at=? WHERE id = ?",
                [("父版本生成失败，修订未执行", int(time.time()), row["id"]) for row in rows],
            )
        con.commit()
    finally:
        con.close()
    if succeeded:
        for row in rows:
            enqueue(row["id"])
    else:
        for row in rows:
            _settle_waiting_revisions(row["id"], False)


def _fail_deck(deck_id: int, error: str) -> None:
    """Fail one Deck and deterministically settle any dependent revision."""
    _user_canceled.discard(deck_id)
    _set_status(
        deck_id,
        "failed",
        error=error,
        finished_at=int(time.time()),
    )
    _settle_waiting_revisions(deck_id, False)


def _ensure_static_delivery(row, run_dir: Path) -> str | None:
    """Materialize the canonical player before a static Deck is completed.

    Agents normally run the Skill's delivery step themselves, but rendered
    pages alone are not a complete downloadable presentation.  Keep this
    postcondition in Studio as a deterministic safety net so a forgotten final
    tool call cannot leave a completed Deck without ``present.html`` or its
    subset font assets.
    """
    try:
        seed = json.loads(row["seed_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        seed = {}
    if seed.get("ppt_output") == "dynamic_html":
        deck = run_dir / "deck.html"
        return None if deck.is_file() and deck.stat().st_size else "最终交付物缺少 deck.html"
    present = run_dir / "present.html"
    slide_files = sorted(
        path for path in (run_dir / "slides").glob("slide_*.html")
        if re.fullmatch(r"slide_\d+\.html", path.name)
    )
    if not slide_files:
        return "最终交付物缺少 present.html，且没有可用于重建的页面 HTML"

    scripts: list[tuple[str, Path]] = []
    for path in sorted((run_dir / "skills").glob("*/scripts/deck.py")):
        scripts.append(("deck", path))

    skill_key = engine.canon_skill(row["skill_version"] if row else None)
    skill = engine.SKILLS.get(skill_key or "") or {}
    skill_root = Path(str(skill.get("path") or ""))
    deck_script = skill_root / "scripts" / "deck.py"
    player_script = skill_root / "scripts" / "build_player.py"
    if deck_script.is_file() and all(path != deck_script for _, path in scripts):
        scripts.append(("deck", deck_script))
    if player_script.is_file():
        scripts.append(("player", player_script))

    attempts: list[str] = []
    delivery_python = engine._engine_python() or sys.executable

    # Presenter 交付必须通过其自身的 portable dependency audit。先审计已有
    # present；若失败则由 build 原子补齐/规范运行时资产，再审一次。
    if skill_key in {"sn-ppt-web", "mural-presenter"} and deck_script.is_file():
        expected = str(len(slide_files))

        def run_deck(command: str):
            return subprocess.run(
                [delivery_python, str(deck_script), command, str(run_dir), "--expected", expected],
                cwd=run_dir,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )

        try:
            audit = run_deck("audit") if present.is_file() and present.stat().st_size else None
            if audit is not None and audit.returncode == 0:
                return None
            built = run_deck("build")
            if built.returncode == 0:
                audit = run_deck("audit")
                if audit.returncode == 0:
                    try:
                        with engine.log_path(int(row["id"])).open("a", encoding="utf-8") as log:
                            log.write("\n[studio] 已校验 present.html 并补齐 Deck 本地运行时依赖\n")
                    except OSError:
                        pass
                    return None
            detail = (
                (audit.stderr or audit.stdout) if audit is not None and audit.returncode
                else (built.stderr or built.stdout or f"exit={built.returncode}")
            ).strip()
            return "最终交付依赖不完整，自动修复失败：" + detail[-1200:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"最终交付依赖审计失败：{type(exc).__name__}: {exc}"

    if present.is_file() and present.stat().st_size:
        return None
    for kind, script in scripts:
        try:
            if kind == "deck":
                bootstrap = (
                    "import importlib.util,pathlib,sys;"
                    "p=pathlib.Path(sys.argv[1]).resolve();"
                    "sys.path.insert(0,str(p.parent));"
                    "s=importlib.util.spec_from_file_location('_studio_delivery',p);"
                    "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                    "m.build(pathlib.Path(sys.argv[2]).resolve())"
                )
                command = [delivery_python, "-c", bootstrap, str(script), str(run_dir)]
            else:
                command = [delivery_python, str(script), "slides", "present.html"]
            result = subprocess.run(
                command,
                cwd=run_dir,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            attempts.append(f"{script.name}: {exc}")
            continue
        if result.returncode == 0 and present.is_file() and present.stat().st_size:
            try:
                with engine.log_path(int(row["id"])).open("a", encoding="utf-8") as log:
                    log.write("\n[studio] 已补齐最终 present.html 与便携字体资源\n")
            except OSError:
                pass
            return None
        detail = (result.stderr or result.stdout or f"exit={result.returncode}").strip()
        attempts.append(f"{script.name}: {detail[-800:]}")

    suffix = "；".join(attempts) if attempts else "没有找到可用的 Skill 交付脚本"
    return f"最终交付物缺少 present.html，自动补齐失败：{suffix}"


def _finalize(deck_id, run_dir: Path, rc=None):
    """Decide final status from result.json + what actually landed on disk.

    Some engine rejections only describe trace/SFT quality and a rendered deck
    can still be useful.  Pixel-review and delivery-integrity rejections are
    different: existing PNGs are not a valid delivery when the final Review did
    not finish or inspect fresh pixels.
    """
    res_path = run_dir / "result.json"
    engine_status, reason, err = None, None, None
    if res_path.exists():
        try:
            res = json.loads(res_path.read_text(encoding="utf-8"))
            engine_status = res.get("status")
            reason = res.get("reason")
            err = res.get("error")
        except Exception as e:
            err = f"bad result.json: {e}"
    else:
        err = f"engine produced no result.json (exit={rc}); see {engine.log_path(deck_id)}"

    row = _load_deck(deck_id)
    try:
        seed = json.loads(row["seed_json"] or "{}") if row else {}
    except (TypeError, ValueError):
        seed = {}
    dynamic_v2 = seed.get("ppt_output") == "dynamic_html"
    rendered = len(glob.glob(str(run_dir / "renders" / "slide_*.png")))
    if dynamic_v2:
        rendered = max(
            len(glob.glob(str(run_dir / "shots" / "page_*.png"))),
            len(glob.glob(str(run_dir / "shots" / "slide_*.png"))),
        )
    is_revision = bool(row and _revision_seed(row))
    # A user stop is authoritative even when the engine managed to leave a
    # render/result behind before the termination signal was observed.  The
    # old order treated "rendered > 0" as success first and therefore turned a
    # stopped task back into "completed" during finalization.
    if deck_id in _user_canceled:
        _user_canceled.discard(deck_id)
        _set_status(
            deck_id,
            "interrupted",
            error="用户已停止",
            finished_at=int(time.time()),
        )
        _settle_waiting_revisions(deck_id, False)
        return

    rejection_text = " ".join(str(value or "") for value in (reason, err)).lower()
    delivery_blocking_markers = (
        "最终 review", "没有执行最终 review", "review", "vision_analyze",
        "final_pixels_inspected", "交付依赖审计", "没有成功渲染",
        "渲染疑似空白", "render freshness",
    )
    delivery_blocked = (
        engine_status == "rejected"
        and any(marker in rejection_text for marker in delivery_blocking_markers)
    )
    succeeded = (
        engine_status == "completed"
        or (not is_revision and rendered > 0 and not delivery_blocked)
    )
    if succeeded:
        delivery_error = _ensure_static_delivery(row, run_dir)
        if delivery_error:
            _fail_deck(deck_id, delivery_error)
            return
    if succeeded:
        _user_canceled.discard(deck_id)
        _set_status(deck_id, "completed", slide_count=rendered or None,
                    error=None, finished_at=int(time.time()))
    else:
        _fail_deck(deck_id, err or reason or "生成失败(无成稿)")
        return
    _settle_waiting_revisions(deck_id, succeeded)


async def _process(deck_id):
    row = _load_deck(deck_id)
    # A queued item can still be present in the dispatch queue after the user
    # stops it.  Do not let that stale queue entry revive an interrupted task.
    if not row or row["status"] != "queued":
        return
    run_dir = Path(row["run_dir"])
    seed = json.loads(row["seed_json"])
    revision = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else None
    if revision:
        _prepare_revision_workspace(row, revision)
    dry = bool(seed.pop("_dry", False))     # _dry is studio-only, not part of the engine seed
    sample_id = f"u{row['user_id']}_d{deck_id}"

    model_key = row["model"] if "model" in row.keys() and row["model"] else None
    pipeline_key = row["pipeline"] if "pipeline" in row.keys() and row["pipeline"] else None
    skill_key = row["skill_version"] if "skill_version" in row.keys() and row["skill_version"] else None
    model_config = None
    if custom_models.id_from_key(model_key) is not None:
        con = connect()
        try:
            custom_row = custom_models.get_owned(
                con, row["user_id"], model_key, active_only=False
            )
        finally:
            con.close()
        if not custom_row:
            raise ValueError("自定义模型不存在或不属于当前用户")
        model_config = custom_models.runtime_config(custom_row)
    job = engine.build_job(sample_id, seed, run_dir, dry=dry, model_key=model_key,
                           pipeline_key=pipeline_key, skill_key=skill_key,
                           model_config=model_config)
    job_path = engine.write_job_file(deck_id, job)
    logf = open(engine.log_path(deck_id), "wb")

    _set_status(
        deck_id,
        "running",
        started_at=int(time.time()),
        finished_at=None,
        error=None,
    )
    rc = None
    # Drop studio's venv vars so `uv run --project inference` uses the engine's
    # own venv cleanly (avoids the harmless VIRTUAL_ENV-mismatch warning).
    # Also drop inherited ANTHROPIC_*/OPENAI_* so the engine's own inference/.env
    # (loaded via setdefault in distill.load_dotenv) is authoritative. Without this,
    # an ambient ANTHROPIC_BASE_URL=https://api.anthropic.com (e.g. from a parent
    # Claude harness) shadows the tokenhub endpoint and the tokenhub key 401s.
    # (teammate-B deploy 2026-06-29)
    _DROP = ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT",
             "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
             "OPENAI_API_KEY", "OPENAI_BASE_URL", "IMAGE_API_KEY", "IMAGE_BASE_URL",
             "IMAGE_MODEL", "IMAGE_PROVIDER",
             "SERPER_API_KEY", "SERPER_BASE_URL",
             # Limits are account-scoped.  Never inherit another launcher or
             # user's process-level values when 0 means Harness default.
             "MAX_TURNS", "CLEAN_MAX_TURNS", "CLEAN_CHILD_MAX_TURNS",
             "SUBAGENT_MAX_TURNS", "STUDIO_MAX_TURNS")
    _DROP = _DROP + (
        "STUDIO_THINKING_TRANSPORT",
        "STUDIO_REQUESTED_THINKING",
        "STUDIO_EFFECTIVE_THINKING",
        "STUDIO_ENABLE_THINKING",
        "THINKING",
    )
    child_env = {k: v for k, v in os.environ.items() if k not in _DROP}
    child_env.update(engine.selection_env(
        model_key, pipeline_key, skill_key, model_config=model_config
    ))
    # Deployment-owned defaults come from namespaced environment variables;
    # user-owned WebUI settings override them for this job only.
    # API keys never enter job.json/result.json and are not process-global.
    child_env.update(service_config.system_runtime_env())
    child_env.update(service_config.runtime_env(row["user_id"]))
    # All WebDemo static Harnesses use the same per-turn completion budget.
    # Their historical defaults differ (VisualCraft 16K, Clean 32K), so pass
    # every compatibility variable explicitly instead of relying on defaults.
    limits = seed.get("runtime_limits")
    if not isinstance(limits, dict):
        limits = service_config.generation_limits(row["user_id"])
    token_budget = str(limits.get("max_tokens") or 40960)
    for token_env in (
        "MAX_TOKENS",
        "SUBAGENT_MAX_TOKENS",
        "CLEAN_MAX_TOKENS",
        "STUDIO_MAX_TOKENS",
    ):
        child_env[token_env] = token_budget
    static_max_turns = int(limits.get("static_max_turns") or 0)
    if static_max_turns:
        turn_budget = str(static_max_turns)
        child_env.update({
            "MAX_TURNS": turn_budget,
            "CLEAN_MAX_TURNS": turn_budget,
        })
    static_subagent_max_turns = int(limits.get("static_subagent_max_turns") or 0)
    if static_subagent_max_turns:
        child_turn_budget = str(static_subagent_max_turns)
        child_env.update({
            "SUBAGENT_MAX_TURNS": child_turn_budget,
            "CLEAN_CHILD_MAX_TURNS": child_turn_budget,
        })
    # Studio owns the user-facing Think switch.  Always pass an explicit value
    # so self-hosted OpenAI-compatible endpoints cannot silently fall back to
    # their server-side default (which may be thinking-on).
    requested_thinking = bool(seed.get("requested_thinking", seed.get("thinking")))
    effective_thinking = bool(seed.get("effective_thinking", seed.get("thinking")))
    child_env["STUDIO_REQUESTED_THINKING"] = "1" if requested_thinking else "0"
    child_env["STUDIO_EFFECTIVE_THINKING"] = "1" if effective_thinking else "0"
    child_env["STUDIO_ENABLE_THINKING"] = child_env["STUDIO_EFFECTIVE_THINKING"]
    child_env["THINKING"] = child_env["STUDIO_EFFECTIVE_THINKING"]
    child_env = engine.render_env(child_env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *engine.runner_cmd(job_path),
            stdout=logf, stderr=asyncio.subprocess.STDOUT,
            cwd=str(engine.DISTILL_DIR), env=child_env,
            start_new_session=True,   # 独立会话:studio 重启/被杀不会连带 TERM 引擎(曾致 exit=143 秒败)
        )
        _running[deck_id] = proc
        spawn_ts = time.time()
        waiter = asyncio.ensure_future(proc.wait())
        res_path = run_dir / "result.json"

        def _result_fresh():
            # retry 复用 run_dir、不清旧产物 → 只认本次 spawn 之后写的 result.json,
            # 否则上一轮失败残留的 result.json 会让早收尾误杀刚起的新引擎。
            try:
                return res_path.stat().st_mtime >= spawn_ts - 1
            except OSError:
                return False

        # 早收尾:result.json 是引擎业务上的最后一笔;之后进程若迟迟不退,只可能是
        # 被超时遗弃的僵尸子线程拖住(非 daemon 线程阻塞解释器退出,期间还在白烧
        # 模型 API/GPU)。deck36 实测:内容 76min 做完,进程 230min 才退。
        # → 见到新 result.json 后给 10s 自然退出,仍滞留则终止进程止损。
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=3)
            if done:
                rc = waiter.result()
                break
            if _result_fresh():
                done, _ = await asyncio.wait({waiter}, timeout=10)
                if done:
                    rc = waiter.result()
                    break
                print(f"[jobs] deck {deck_id}: result.json 已落盘但引擎滞留(僵尸子线程),终止止损",
                      flush=True)
                proc.terminate()
                done, _ = await asyncio.wait({waiter}, timeout=8)
                if not done:
                    proc.kill()
                    await waiter
                rc = waiter.result() if waiter.done() else None
                break
    except Exception as e:
        _fail_deck(deck_id, f"launch error: {type(e).__name__}: {e}")
        return
    finally:
        _running.pop(deck_id, None)
        logf.close()

    # studio 关停时 asyncio 会 TERM 它的直接子进程(uv 包装层,rc=143),
    # 但引擎(孙进程,独立会话)往往还活着 —— 此时转入领养观察,绝不能误标 failed。
    if _engine_alive(deck_id):
        await _adopt(deck_id, run_dir)
        return
    _finalize(deck_id, run_dir, rc)


def _kill_engine_procs(deck_id) -> bool:
    """TERM this deck's inference worker on Linux, macOS, or Windows."""
    killed = False
    for pid in _matching_engine_pids(deck_id):
        try:
            os.kill(pid, signal.SIGTERM)
            killed = True
        except OSError:
            continue
    return killed


async def _adopt(deck_id, run_dir: Path):
    """Watch an engine process that survived a studio restart; finalize when done."""
    res_path = run_dir / "result.json"
    row = _load_deck(deck_id)
    started_at = (row["started_at"] or 0) if row else 0

    def _result_fresh():
        # 领养的 deck 也可能是 retry 复用的 run_dir → 只认本次 started_at 之后的 result.json
        try:
            return res_path.stat().st_mtime >= started_at - 1
        except OSError:
            return False

    lingered = 0
    while _engine_alive(deck_id):
        if _result_fresh():
            lingered += 1
            if lingered >= 3:      # ~15s 宽限:业务已收尾、进程仍滞留 → 同 _process,终止止损
                print(f"[jobs] deck {deck_id}(adopted): result.json 已落盘但引擎滞留,终止止损",
                      flush=True)
                _kill_engine_procs(deck_id)
                break
        await asyncio.sleep(5)
    # small grace period for result.json to land after process exit
    for _ in range(4):
        if res_path.exists():
            break
        await asyncio.sleep(1)
    _finalize(deck_id, run_dir)


async def _worker_loop(queue):
    while True:
        deck_id = await queue.get()
        try:
            await _process(deck_id)
        except Exception as e:
            _fail_deck(deck_id, f"dispatcher: {type(e).__name__}: {e}")
        finally:
            queue.task_done()


async def _run_scheduled(deck_id):
    """Run one immediately scheduled Deck and clear its deduplication guard."""
    try:
        row = _load_deck(deck_id)
        if not row or row["status"] != "queued":
            return
        await _process(deck_id)
    except Exception as e:
        _fail_deck(deck_id, f"dispatcher: {type(e).__name__}: {e}")
    finally:
        _scheduled.discard(deck_id)


def _schedule_unbounded(deck_id):
    if deck_id in _scheduled or deck_id in _running or _engine_alive(deck_id):
        return
    _scheduled.add(deck_id)
    task = asyncio.create_task(_run_scheduled(deck_id))
    _workers.append(task)
    task.add_done_callback(lambda done: _workers.remove(done) if done in _workers else None)


def _model_key(deck_id) -> str:
    row = _load_deck(deck_id)
    if row and custom_models.id_from_key(row["model"]) is not None:
        # 自定义模型走默认 OpenAI 队列，运行时仍按 deck 读取各自 URL / Model ID / Key。
        return engine.DEFAULT_MODEL
    ck = engine.canon(row["model"]) if row and "model" in row.keys() else None
    return ck or engine.DEFAULT_MODEL   # 老 deck 的旧 key 经 alias 归一


def enqueue(deck_id):
    if not _queues or _loop is None:
        return
    if MAX_PER_MODEL <= 0:
        _loop.call_soon_threadsafe(_schedule_unbounded, deck_id)
        return
    queue = _queues[_model_key(deck_id)]
    _loop.call_soon_threadsafe(queue.put_nowait, deck_id)


async def cancel(deck_id) -> bool:
    _user_canceled.add(deck_id)
    row = _load_deck(deck_id)
    if row and row["status"] in ("waiting", "queued"):
        _set_status(
            deck_id,
            "interrupted",
            error="用户已停止",
            finished_at=int(time.time()),
        )
        _user_canceled.discard(deck_id)
        return True
    if row and row["status"] == "running":
        # Make the interruption visible immediately.  _finalize() keeps this
        # state authoritative after the subprocess exits.
        _set_status(
            deck_id,
            "interrupted",
            error="用户已停止",
            finished_at=int(time.time()),
        )
    proc = _running.get(deck_id)
    if proc is not None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        return True
    # adopted run (no handle): kill by scanning /proc
    return _kill_engine_procs(deck_id)


async def start():
    """Init dispatch state, recover/adopt orphaned runs, and start queued Decks."""
    global _queues, _loop
    _loop = asyncio.get_running_loop()
    _queues = {k: asyncio.Queue() for k in engine.MODELS}

    con = connect()
    try:
        running = [dict(r) for r in con.execute(
            "SELECT id, run_dir FROM decks WHERE status = 'running'")]
        queued = [r["id"] for r in con.execute(
            "SELECT id FROM decks WHERE status = 'queued' ORDER BY id")]
        waiting = [dict(r) for r in con.execute(
            "SELECT child.id, parent.status AS parent_status "
            "FROM decks child LEFT JOIN decks parent ON parent.id = child.parent_deck_id "
            "WHERE child.status = 'waiting' ORDER BY child.id"
        )]
    finally:
        con.close()

    if MAX_PER_MODEL > 0:
        for q in _queues.values():
            for _ in range(MAX_PER_MODEL):
                _workers.append(asyncio.create_task(_worker_loop(q)))

    for r in running:
        run_dir = Path(r["run_dir"] or "")
        if _engine_alive(r["id"]):
            # engine survived the restart — watch it, do NOT spawn a second one
            _workers.append(asyncio.create_task(_adopt(r["id"], run_dir)))
        elif (run_dir / "result.json").exists():
            _finalize(r["id"], run_dir)
        else:
            _set_status(r["id"], "queued")
            queued.append(r["id"])

    for item in waiting:
        if item["parent_status"] == "completed":
            _set_status(item["id"], "queued", error=None)
            queued.append(item["id"])
        elif item["parent_status"] in ("failed", "rejected", None):
            _fail_deck(item["id"], "父版本未成功完成，修订未执行")

    for did in sorted(set(queued)):
        enqueue(did)
