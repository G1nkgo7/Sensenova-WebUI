#!/usr/bin/env python3
"""单条 live 生成入口 —— 给 studio 后端调用,跑一份 PPT。

不引入并行 / manifest / 进度条;**直接复用 `runtime.worker` 的单样本逻辑**
(load_dotenv / ANTHROPIC_API_KEY 检查 / exist_ok=False 隔离 + 脏残留清理 / 异常捕获),
跑完把结果 rec 写到 <run_dir>/result.json,供 studio dispatcher 读取判定状态。

由 studio 的 dispatcher 在 **inference 的 uv 环境**里起(这样能 import agent_loop 的
重依赖:anthropic / playwright …):

    uv run --project <inference> python <inference>/serve_one.py --job /abs/job.json

job.json:
    {"sample_id": "u1_d7", "seed": {"query": "...", "lang": "zh", "slide_count": 12},
     "run_dir": "/abs/.../decks/7", "dry_run": false, "batch": "studio"}
"""
import argparse
import importlib.util
import importlib
import inspect
import json
import os
import sys
import time
import traceback
import types
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import runtime as runtime_driver  # noqa: E402  (reuse worker/build_config/load_dotenv)
from attachments_runtime import build_initial_user_content, stage_seed_attachments  # noqa: E402


def _install_thinking_transport():
    """Inject the Studio Think choice into versioned OpenAI shims.

    Versioned Harnesses intentionally remain immutable and each carries its
    own ``core.openai_backend`` module.  They all use ``urllib.request.Request``
    for chat completions, so this process-local adapter applies the request
    option without rewriting every frozen Harness copy.
    """
    if os.environ.get("STUDIO_THINKING_TRANSPORT") != "chat_template_kwargs":
        return
    original = urllib.request.Request
    if getattr(original, "_studio_thinking_adapter", False):
        return
    enabled = os.environ.get(
        "STUDIO_EFFECTIVE_THINKING",
        os.environ.get("STUDIO_ENABLE_THINKING", "0"),
    ) == "1"

    class StudioRequest(original):
        """Request-compatible adapter that preserves urllib's class contract.

        httpx subclasses ``urllib.request.Request`` while it is imported.  A
        function wrapper therefore breaks unrelated Harness imports with
        ``TypeError: function() argument 'code' must be code, not str``.  Keep
        the transport hook as a real subclass so both direct construction and
        third-party subclassing continue to work.
        """

        _studio_thinking_adapter = True

        def __init__(self, url, data=None, headers=None, origin_req_host=None,
                     unverifiable=False, method=None):
            target = getattr(url, "full_url", url)
            if data and str(target).rstrip("/").endswith("/chat/completions"):
                try:
                    payload = json.loads(
                        data.decode("utf-8") if isinstance(data, bytes) else data
                    )
                    payload["chat_template_kwargs"] = {"enable_thinking": enabled}
                    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                except (TypeError, ValueError, UnicodeDecodeError):
                    pass
            super().__init__(
                url,
                data=data,
                headers=headers or {},
                origin_req_host=origin_req_host,
                unverifiable=unverifiable,
                method=method,
            )

    urllib.request.Request = StudioRequest


def _load_module(name, path, root):
    """Load a pipeline module from an explicit file path with its root on sys.path."""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module spec: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _select_pipeline(job):
    """Return the inference module for this job and apply any version-specific wiring."""
    version = job.get("pipeline_version") or "current"
    pipe = job.get("pipeline") or {}
    root = pipe.get("path")
    entry = pipe.get("entry") or "presenter.py"
    if not root:
        raise RuntimeError(f"pipeline {version} missing path")
    skill_mode = pipe.get("skill_mode") or "ppt-skill-html"
    skill_mount = job.get("skill_mount_dir")
    # Set path contracts before importing the external Harness: both the V2
    # thin Harness and the legacy Web Demo read these values at import/runtime.
    if skill_mode == "ppt-skill":
        skill_path = (job.get("skill") or {}).get("path") or skill_mount
        if skill_path:
            os.environ["PPT_SKILL_DIR"] = os.path.abspath(skill_path)
    elif skill_mount:
        os.environ["PPT_SKILLS_ROOT"] = os.path.abspath(skill_mount)
    safe_version = "".join(ch if ch.isalnum() else "_" for ch in version)
    mod = _load_module(
        f"pptagent_pipeline_{safe_version}", os.path.join(root, entry), root
    )
    # Every catalog entry reads the logical skills/ppt-skill-html root. Point it
    # at the mount generated for the selected Skill version.
    if skill_mode == "ppt-skill-html" and skill_mount:
        mod.SKILLS_DIR = os.path.abspath(skill_mount)
    return mod


def _install_subagent_turn_adapter(job):
    """Apply Studio's per-child turn limit to versioned external Harnesses.

    Current in-repo and Clean Harnesses have native child-limit fields.  Some
    immutable/versioned Harnesses still clone the orchestrator config for each
    child (and a few impose a role-specific constant afterwards).  Patch the
    loaded Agent class for this one worker process so every delegated Agent
    receives the account's explicit limit and writes it to its trace config.
    """
    raw_limit = os.environ.get("SUBAGENT_MAX_TURNS", "").strip()
    if not raw_limit:
        return
    try:
        limit = int(raw_limit)
    except ValueError:
        return
    if limit <= 0:
        return
    root = os.path.abspath((job.get("pipeline") or {}).get("path") or "")
    if not os.path.isfile(os.path.join(root, "core", "agent.py")):
        return
    try:
        agent_module = importlib.import_module("core.agent")
        agent_cls = agent_module.Agent
    except (ImportError, AttributeError):
        return
    original = agent_cls.__init__
    if getattr(original, "_studio_subagent_turn_adapter", False):
        return
    signature = inspect.signature(original)

    def wrapped(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        role = bound.arguments.get("role")
        if role != "orchestrator":
            config = dict(bound.arguments.get("config") or {})
            config["max_turns"] = limit
            bound.arguments["config"] = config
        return original(*bound.args, **bound.kwargs)

    wrapped._studio_subagent_turn_adapter = True
    agent_cls.__init__ = wrapped


def _write_result(run_dir, rec, job, started):
    """Persist the common Studio completion contract for every Harness."""
    rec["duration_s"] = round(time.time() - started, 1)
    rec["pipeline_version"] = job.get("pipeline_version", "infer")
    rec["skill_version"] = job.get("skill_version")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return rec


def _run_clean_infer(job):
    """Run one Studio job through static_ppt-clean-current/infer.py.

    infer.py owns the render broker and core.run_batch owns the one-deck worker.
    Studio supplies an exact run_dir, so calling the worker directly preserves
    the Web Demo workspace/result.json contract without creating an extra
    runs/<batch>/<sample> nesting layer.
    """
    pipe = job.get("pipeline") or {}
    root = os.path.abspath(pipe.get("path") or "")
    entry = pipe.get("entry") or "infer.py"
    if not root or entry != "infer.py":
        raise RuntimeError("clean infer pipeline is missing its infer.py entry")
    infer_path = os.path.join(root, entry)
    if not os.path.isfile(infer_path):
        raise RuntimeError(f"clean infer entry does not exist: {infer_path}")

    sample_id = job["sample_id"]
    seed = dict(job["seed"])
    run_dir = job["run_dir"]
    started = time.time()

    if job.get("dry_run"):
        os.makedirs(run_dir, exist_ok=True)
        rec = {
            "sample_id": sample_id,
            "run_dir": run_dir,
            "status": "completed",
            "dry_run": True,
            "skill_name": (job.get("skill") or {}).get("name"),
            "skill_language": (job.get("skill") or {}).get("language"),
        }
        return _write_result(run_dir, rec, job, started)

    infer = _load_module("pptagent_clean_infer", infer_path, root)
    from core import model_call, run_batch  # noqa: E402

    if os.environ.get("MODEL_BACKEND", "").lower() == "openai":
        import openai_backend  # noqa: E402

        shim = openai_backend.OpenAIShim(
            base=os.environ.get("STUDENT_BASE_URL", ""),
            model=os.environ.get("STUDENT_MODEL") or job.get("model"),
            key=os.environ.get("STUDENT_API_KEY", "EMPTY"),
            timeout=int(os.environ.get("CLEAN_MODEL_TIMEOUT", "600")),
        )
        model_call._client = lambda: shim

    cfg_args = types.SimpleNamespace(
        batch=job.get("batch", "studio"),
        workers=1,
        max_attempts=1,
    )
    config = run_batch.build_config(cfg_args)
    config["model"] = job.get("model") or config["model"]
    config["requested_thinking"] = bool(
        seed.get("requested_thinking", seed.get("thinking"))
    )
    config["effective_thinking"] = bool(
        seed.get("effective_thinking", seed.get("thinking"))
    )
    config["thinking_transport"] = str(seed.get("thinking_transport") or "")
    config["thinking"] = config["effective_thinking"]
    skill = job.get("skill") or {}
    if skill.get("language") == "auto":
        config["skill_name"], config["skill_language"] = run_batch.select_skill(seed)
    else:
        config["skill_name"] = skill.get("name")
        config["skill_language"] = skill.get("language")
    task = {
        "sample_id": sample_id,
        "seed": seed,
        "run_dir": run_dir,
        "config": config,
    }

    infer._load_render_environment()
    infer._configure_runtime_environment()
    broker_process, broker_dir = infer._start_render_broker()
    try:
        worker = run_batch.revision_worker if seed.get("_revision") else run_batch.worker
        rec = worker(task)
    except Exception as exc:
        rec = {
            "sample_id": sample_id,
            "run_dir": run_dir,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-1500:],
        }
    finally:
        infer._stop_render_broker(broker_process, broker_dir)
    selected_name = rec.get("skill_name") or config.get("skill_name")
    selected_version = rec.get("skill_language") or config.get("skill_language")
    # clean run_batch.worker keeps its pre-Agent config fields. Auto's actual
    # first-turn choice is authoritative in the orchestrator trace config.
    try:
        with open(
            os.path.join(run_dir, "_trace", "orchestrator", "config.json"),
            encoding="utf-8",
        ) as f:
            trace_config = json.load(f)
        selected_name = trace_config.get("skill_name") or selected_name
        selected_version = trace_config.get("skill_language") or selected_version
    except (OSError, ValueError, TypeError):
        pass
    rec["skill_name"] = selected_name
    rec["skill_language"] = selected_version
    rec["selected_skill_name"] = selected_name
    rec["selected_skill_version"] = selected_version
    return _write_result(run_dir, rec, job, started)


def _patch_attachment_staging(pipe):
    """Wrap versioned pipeline run_sample so image attachments land in work_dir.

    A catalog harness can run on a local scratch work_dir before persisting back to the final
    run_dir, so staging has to happen inside run_sample rather than before worker().
    The current pipeline stages in runtime.worker; wrapping it here is harmless only
    when the module exposes run_sample directly.
    """
    run_sample = getattr(pipe, "run_sample", None)
    if not callable(run_sample) or getattr(pipe, "_studio_attachment_staging", False):
        return

    def wrapped(sample_id, seed, run_dir, config):
        stage_seed_attachments(seed, run_dir)
        return run_sample(sample_id, seed, run_dir, config)

    pipe.run_sample = wrapped
    pipe._studio_attachment_staging = True


def _patch_attachment_message_content(pipe):
    """Patch versioned pipelines so staged images become initial message image blocks."""
    seed_to_brief = getattr(pipe, "seed_to_brief", None)
    if not callable(seed_to_brief) or getattr(pipe, "_studio_attachment_message_content", False):
        return

    def wrapped(seed, *args, **kwargs):
        # Versioned Harnesses do not share one seed_to_brief signature.  The
        # long-horizon presenter passes its staged attachment manifest as a
        # second positional argument; preserve every argument while adding the
        # Studio image blocks around the returned brief.
        return build_initial_user_content(seed, seed_to_brief(seed, *args, **kwargs))

    pipe.seed_to_brief = wrapped
    pipe._studio_attachment_message_content = True


def _patch_optional_harness_dotenv(pipe):
    """Treat a version Harness .env as optional; Studio injects credentials."""
    original = getattr(pipe, "load_dotenv", None)
    if not callable(original) or getattr(pipe, "_studio_optional_dotenv", False):
        return

    def wrapped():
        try:
            return original()
        except PermissionError as exc:
            print(f"[serve_one] skip unreadable harness .env: {exc}", file=sys.stderr)
            return None

    pipe.load_dotenv = wrapped
    pipe._studio_optional_dotenv = True


def main():
    ap = argparse.ArgumentParser(description="单条 live PPT 生成(供 studio 调用)")
    ap.add_argument("--job", required=True, help="job spec json 路径")
    args = ap.parse_args()

    with open(args.job, encoding="utf-8") as f:
        job = json.load(f)

    _install_thinking_transport()

    sample_id = job["sample_id"]
    seed = job["seed"]
    run_dir = job["run_dir"]
    dry = bool(job.get("dry_run"))

    if (job.get("pipeline") or {}).get("skill_mode") == "clean-bilingual":
        rec = _run_clean_infer(job)
        print(json.dumps({
            "status": rec.get("status"),
            "run_dir": run_dir,
            "n_slides": rec.get("n_slides", rec.get("n_renders")),
        }, ensure_ascii=False))
        sys.exit(0 if rec.get("status") == "completed" else 1)

    pipe = _select_pipeline(job)
    _install_subagent_turn_adapter(job)
    _patch_attachment_staging(pipe)
    _patch_attachment_message_content(pipe)
    _patch_optional_harness_dotenv(pipe)
    # Studio credentials live in this repo's inference/.env and are also
    # injected by jobs.py. Frozen/versioned Harness directories intentionally
    # do not own readable credentials, so their optional .env must not block a
    # run when the process environment is already complete.
    runtime_driver.load_dotenv()
    pipe.load_dotenv()

    # 复用 inference 的标准 config;dry_run 走 runtime.worker 里的 _dry_worker 分支。
    cfg_args = types.SimpleNamespace(batch=job.get("batch", "studio"), dry_run=dry)
    config = pipe.build_config(cfg_args)
    for k in ("model", "max_turns", "max_tokens"):   # 允许 job 覆盖少量参数
        if job.get(k) is not None and k in config:
            config[k] = job[k]
    config["pipeline_version"] = job.get("pipeline_version", "current")
    config["skill_version"] = job.get("skill_version")
    config["thinking"] = bool(seed.get("thinking"))

    task = {"sample_id": sample_id, "seed": seed, "run_dir": run_dir, "config": config}

    started = time.time()
    try:
        if seed.get("_revision"):
            worker = getattr(pipe, "revision_worker", None)
            if not callable(worker):
                raise RuntimeError("所选 Harness 尚未实现 revision_worker")
        else:
            worker = pipe.worker
        rec = worker(task)                    # ← pipeline 的单样本调用(隔离/容错都在里面)
    except Exception as e:                    # worker 已自捕获,这层只是最后兜底
        rec = {"sample_id": sample_id, "run_dir": run_dir, "status": "error",
               "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
    rec["duration_s"] = round(time.time() - started, 1)
    rec["pipeline_version"] = job.get("pipeline_version", "current")
    rec["skill_version"] = job.get("skill_version")

    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)

    print(json.dumps({"status": rec.get("status"), "run_dir": run_dir,
                      "n_slides": rec.get("n_slides", rec.get("slides"))}, ensure_ascii=False))
    sys.exit(0 if rec.get("status") == "completed" else 1)


if __name__ == "__main__":
    main()
