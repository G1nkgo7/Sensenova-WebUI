"""Read-only browser for external Mural Presenter batch trajectories.

The monitor deliberately does not register, mutate, resume, or cancel jobs.  It
only projects files already written by the inference harness into a compact
batch/sample/stage view for the Studio UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from . import mdrender, trace


BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter(tags=["trajectory-monitor"])

DEFAULT_ROOTS = (
    Path("/mnt/afs/hejiatong/qc-workspace/mural-presenter-smoke"),
    Path("/mnt/afs/hejiatong/qc-workspace/mural-presenter-batches"),
)
DEFAULT_NOVA_RAW_ROOTS = (
    Path("/mnt/afs/multimodal_data/hejiatong/long-ppt/nova_raw"),
)
DEFAULT_QUERY_CATALOG = Path(
    "/mnt/afs/hejiatong/multimodal_design/ppt-agent/data/queryGeneration/longhorizon-batch/"
    "realistic-visual-rich-3k-v3-20260808-v2/queries.jsonl"
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".txt", ".log", ".html", ".css"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_QUERY_CACHE: tuple[tuple[str, int, int], list[dict[str, Any]]] | None = None
_CATALOG_RECORD_CACHE: tuple[tuple[str, tuple[str, ...]], float, list[dict[str, Any]]] | None = None


def _asset_version() -> str:
    digest = hashlib.sha256()
    try:
        for name in ("app.css", "app.js"):
            digest.update((BASE_DIR / "static" / name).read_bytes())
    except OSError:
        return "0"
    return digest.hexdigest()[:12]


templates.env.globals["asset_ver"] = _asset_version()


def configured_roots() -> list[Path]:
    raw = str(os.environ.get("STUDIO_TRAJECTORY_ROOTS") or "").strip()
    candidates = [Path(item) for item in raw.split(os.pathsep) if item.strip()] if raw else list(DEFAULT_ROOTS)
    roots: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def configured_nova_raw_roots() -> list[Path]:
    raw = str(os.environ.get("STUDIO_TRAJECTORY_NOVA_RAW_ROOTS") or "").strip()
    candidates = [Path(item) for item in raw.split(os.pathsep) if item.strip()] if raw else list(DEFAULT_NOVA_RAW_ROOTS)
    roots: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _run_id(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"{digest}-{path.name}"


def _deck_id(run: Path, sample: Path) -> int:
    digest = hashlib.sha1(f"{run.resolve()}\0{sample.resolve()}".encode("utf-8")).hexdigest()[:12]
    return int(digest, 16)


def _catalog_deck_id(key: str) -> int:
    digest = hashlib.sha1(f"trajectory-catalog\0{key}".encode("utf-8")).hexdigest()[:12]
    return int(digest, 16)


def _thinking_enabled(config: dict[str, Any]) -> bool:
    effective = config.get("effective_thinking")
    if isinstance(effective, bool):
        return effective
    requested = config.get("requested_thinking")
    if isinstance(requested, bool):
        return requested
    thinking = config.get("thinking")
    if isinstance(thinking, bool):
        return thinking
    if isinstance(thinking, dict):
        effort = str(thinking.get("effort") or "").lower()
        mode = str(thinking.get("type") or "").lower()
        return effort in {"high", "medium", "low"} and mode not in {"disabled", "off", "none"}
    return str(config.get("reasoning_effort") or "").lower() in {"high", "medium", "low"}


def query_catalog_path() -> Path:
    raw = str(os.environ.get("STUDIO_TRAJECTORY_QUERY_CATALOG") or "").strip()
    return Path(raw).expanduser().resolve() if raw else DEFAULT_QUERY_CATALOG


def _query_catalog() -> list[dict[str, Any]]:
    global _QUERY_CACHE
    path = query_catalog_path()
    try:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    if _QUERY_CACHE and _QUERY_CACHE[0] == cache_key:
        return _QUERY_CACHE[1]
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for raw in lines:
        try:
            row = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "").strip()
        query_id = str(row.get("id") or "").strip()
        if not query or not query_id:
            continue
        rows.append(row)
    _QUERY_CACHE = (cache_key, rows)
    return rows


def _matches_trajectory_run(run: Path, config: dict[str, Any]) -> bool:
    """Apply an optional deployment-owned model filter to discovered runs."""
    expected = str(os.environ.get("STUDIO_TRAJECTORY_MODEL_FILTER") or "").strip().lower()
    if not expected:
        return True
    model = str(config.get("model") or "").lower()
    name = run.name.lower()
    return expected in model or expected in name


def _run_directories() -> list[Path]:
    found: list[Path] = []
    for root in configured_roots():
        if (root / "work").is_dir() or (root / "run.log").is_file():
            found.append(root)
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and ((child / "work").is_dir() or (child / "run.log").is_file()):
                found.append(child.resolve())
    return sorted(set(found), key=_latest_mtime, reverse=True)


def _resolve_run(run_id: str) -> Path:
    for path in _run_directories():
        if _run_id(path) == run_id:
            return path
    raise HTTPException(status_code=404, detail="trajectory run not found")


def _normalized_query(value: Any) -> str:
    return " ".join(str(value or "").split())


def _catalog_records() -> list[dict[str, Any]]:
    """Return one row per planned query, joined to the latest matching run."""
    global _CATALOG_RECORD_CACHE
    roots = tuple(str(path) for path in configured_roots())
    cache_key = (str(query_catalog_path()), roots)
    now = time.monotonic()
    if _CATALOG_RECORD_CACHE:
        cached_key, cached_at, cached_rows = _CATALOG_RECORD_CACHE
        if cached_key == cache_key and now - cached_at < 5:
            return cached_rows
    planned = _query_catalog()
    records: dict[str, dict[str, Any]] = {}
    query_to_key: dict[str, str] = {}
    for row in planned:
        query_id = str(row["id"])
        query = _normalized_query(row["query"])
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        config = {
            "sample_id": query_id,
            "task": query,
            "model": str(os.environ.get("STUDIO_TRAJECTORY_DEFAULT_MODEL") or ""),
            "reasoning_effort": "high",
            "generation_preferences": {"page_count": int(row.get("slide_count") or 0)},
            "query_metadata": metadata,
        }
        records[query_id] = {
            "id": _catalog_deck_id(query_id),
            "key": query_id,
            "query_id": query_id,
            "query": query,
            "catalog": row,
            "run": None,
            "sample": None,
            "config": config,
            "updated_at": None,
        }
        query_to_key[query] = query_id

    latest_started: dict[str, tuple[float, Path, Path, dict[str, Any]]] = {}
    for run in _run_directories():
        for sample, config in _sample_roots(run):
            if not _matches_trajectory_run(run, config):
                continue
            query = _normalized_query(config.get("task"))
            sample_id = str(config.get("sample_id") or sample.name)
            key = query_to_key.get(query)
            if key is None:
                key = next((query_id for query_id in records if query_id in sample_id), None)
            if key is None:
                # Keep a real smoke/canary even when it is intentionally
                # outside the formal 3K catalogue. Repeated retries collapse to
                # the same logical query instead of appearing as separate rows.
                key = f"extra-{hashlib.sha1(query.encode('utf-8')).hexdigest()[:16]}"
            timestamp = _latest_mtime(sample)
            previous = latest_started.get(key)
            if previous is None or timestamp > previous[0]:
                latest_started[key] = (timestamp, run, sample, config)

    for key, (timestamp, run, sample, config) in latest_started.items():
        query = _normalized_query(config.get("task"))
        if key not in records:
            records[key] = {
                "id": _catalog_deck_id(key),
                "key": key,
                "query_id": str(config.get("sample_id") or sample.name),
                "query": query,
                "catalog": None,
                "run": None,
                "sample": None,
                "config": config,
                "updated_at": None,
            }
        records[key].update({
            "run": run,
            "sample": sample,
            "config": config,
            "updated_at": _iso(timestamp),
        })
    result = list(records.values())
    _CATALOG_RECORD_CACHE = (cache_key, now, result)
    return result


def _resolve_catalog_deck(deck_id: int) -> dict[str, Any]:
    for record in _catalog_records():
        if record["id"] == deck_id:
            return record
    raise HTTPException(status_code=404, detail="trajectory deck not found")


def _resolve_deck(deck_id: int) -> tuple[Path, Path, dict[str, Any]]:
    record = _resolve_catalog_deck(deck_id)
    if record["run"] is None or record["sample"] is None:
        raise HTTPException(status_code=404, detail="trajectory query has not started")
    return record["run"], record["sample"], record["config"]


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime if path.exists() else 0.0
    for relative in ("run.log", "launcher.pid"):
        candidate = path / relative
        if candidate.exists():
            latest = max(latest, candidate.stat().st_mtime)
    return latest


def _iso(timestamp: float | None) -> str | None:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any) -> float | None:
    """Parse harness timestamps, treating legacy timezone-less values as UTC."""
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace(" UTC", "+00:00").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def _sample_process_alive(run: Path | None, sample: Path) -> bool:
    """Return whether the launcher or any persisted Agent PID is still alive."""
    if run is not None and _pid_alive(_pid(run / "launcher.pid")):
        return True
    config_paths = [sample / "_trace/orchestrator/config.json"]
    config_paths.extend((sample / "_trace/subagents").glob("*/config.json"))
    for config_path in config_paths:
        config = _json(config_path, {})
        try:
            pid = int(config.get("pid")) if isinstance(config, dict) and config.get("pid") else None
        except (TypeError, ValueError):
            pid = None
        if _pid_alive(pid):
            return True
    return False


def _manifest_statuses(run: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for manifest in (run / "logs").glob("*.manifest.jsonl") if (run / "logs").is_dir() else ():
        try:
            for raw in manifest.read_text(encoding="utf-8").splitlines():
                row = json.loads(raw)
                sample_id = str(row.get("sample_id") or "")
                if sample_id:
                    statuses[sample_id] = str(row.get("status") or "unknown")
        except (OSError, ValueError, TypeError):
            continue
    return statuses


def _manifest_record(run: Path, sample_id: str) -> dict[str, Any]:
    """Return the latest terminal record for one sample, when available."""
    latest: dict[str, Any] = {}
    for manifest in (run / "logs").glob("*.manifest.jsonl") if (run / "logs").is_dir() else ():
        try:
            for raw in manifest.read_text(encoding="utf-8").splitlines():
                row = json.loads(raw)
                if str(row.get("sample_id") or "") == sample_id:
                    latest = row
        except (OSError, ValueError, TypeError):
            continue
    return latest


def _sample_roots(run: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    for config_path in run.glob("work/*/*/_trace/orchestrator/config.json"):
        config = _json(config_path, {})
        if isinstance(config, dict):
            rows.append((config_path.parents[2], config))
    return sorted(rows, key=lambda item: _latest_mtime(item[0]), reverse=True)


def _ready_asset_count(sample: Path) -> int:
    catalog = _json(sample / "assets/catalog.json", {})
    entries = catalog.get("assets", []) if isinstance(catalog, dict) else []
    return sum(
        1 for entry in entries
        if isinstance(entry, dict) and str(entry.get("status") or "").lower() == "ready"
    )


def _compact(value: Any, limit: int = 1200) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _active_stream_delta(role_dir: Path, messages: list[Any]) -> str:
    path = role_dir / "live-deltas.jsonl"
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            start = max(0, size - 2 * 1024 * 1024)
            source.seek(start)
            lines = source.read().decode("utf-8", errors="ignore").splitlines()
        if start and lines:
            lines = lines[1:]
    except OSError:
        return ""
    records = []
    for line in lines:
        try:
            item = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(item, dict) and item.get("schema") == "mural.live-delta.v1":
            records.append(item)
    starts = [item for item in records if item.get("event") == "turn_start"]
    if not starts:
        return ""
    current = starts[-1]
    canonical = sum(
        1 for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    try:
        base = int(current.get("base_assistant_count") or 0)
    except (TypeError, ValueError):
        base = 0
    if canonical > base:
        return ""
    stream_id = str(current.get("stream_id") or "")
    return "".join(
        str(item.get("text") or "") for item in records
        if str(item.get("stream_id") or "") == stream_id
        and item.get("event") == "delta" and item.get("kind") == "text"
    ).strip()


def _agent_events(role_dir: Path, limit: int = 100) -> list[dict[str, Any]]:
    """Project harness messages into the same user-visible text/tool stream as Studio."""
    messages = _json(role_dir / "messages.json", [])
    events: list[dict[str, Any]] = []
    if not isinstance(messages, list):
        return events
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "")
            if kind == "text":
                text = _compact(block.get("text"))
                if text:
                    events.append({"kind": "text", "text": text})
            elif kind == "tool_use":
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                hint = args.get("path") or args.get("query") or args.get("pattern") or args.get("aspect_ratio")
                events.append({
                    "kind": "tool",
                    "name": str(block.get("name") or "tool"),
                    "hint": _compact(hint, 240),
                })
    partial = _active_stream_delta(role_dir, messages)
    if partial:
        events.append({"kind": "text", "text": _compact(partial, 4000), "partial": True})
    return events[-limit:]


def _agent_rows(sample: Path, sample_complete: bool) -> list[dict[str, Any]]:
    configs = [sample / "_trace/orchestrator/config.json"]
    configs.extend(sorted((sample / "_trace/subagents").glob("*/config.json")))
    agents: list[dict[str, Any]] = []
    for config_path in configs:
        if not config_path.is_file():
            continue
        config = _json(config_path, {})
        role_dir = config_path.parent
        usage = _json(role_dir / "usage.json", {})
        handoff = _json(role_dir / "handoff.json", {})
        pid = config.get("pid") if isinstance(config, dict) else None
        completion_files = [
            role_dir / name for name in ("messages.json", "tool_log.json", "summary.md", "usage.json", "handoff.json")
            if (role_dir / name).is_file()
        ]
        complete = (role_dir / "usage.json").is_file() or (role_dir / "handoff.json").is_file() or sample_complete
        status = "complete" if complete else ("running" if _pid_alive(pid) else "waiting")
        started_ts = _timestamp(config.get("started_at")) or config_path.stat().st_mtime
        finished_ts = max((path.stat().st_mtime for path in completion_files), default=0.0) if complete else None
        timing_end = finished_ts or (time.time() if status == "running" else None)
        agents.append({
            "label": str(config.get("label") or role_dir.name),
            "role": str(config.get("role") or ("orchestrator" if role_dir.name == "orchestrator" else "worker")),
            "status": status,
            "started_at": _iso(started_ts),
            "finished_at": _iso(finished_ts),
            "duration_s": round(max(0.0, timing_end - started_ts), 1) if timing_end else None,
            "turns": usage.get("n_turns") if isinstance(usage, dict) else None,
            "model_wall_seconds": usage.get("sum_model_wall_seconds") if isinstance(usage, dict) else None,
            "updated_at": _iso(_latest_mtime(role_dir)),
            "events": _agent_events(role_dir),
            "summary": _compact((role_dir / "summary.md").read_text(encoding="utf-8") if (role_dir / "summary.md").is_file() else "", 4000),
        })
    return agents


def _nova_tool_hint(name: str, args: Any) -> str:
    """Return a compact, user-facing hint for one normalized Nova tool call."""
    if not isinstance(args, dict):
        return ""
    if name == "delegate_task":
        label = str(args.get("label") or "协作 Agent").strip()
        goal = str(args.get("goal") or "").strip().splitlines()[0] if args.get("goal") else ""
        return _compact(f"{label}: {goal}" if goal else label, 320)
    for key in ("path", "query", "pattern", "command", "aspect_ratio", "url"):
        value = args.get(key)
        if value:
            return _compact(value, 320)
    return ""


def _visible_nova_text(value: Any) -> str:
    """Keep only explicit assistant prose, never provider thinking/tool payloads."""
    text = str(value or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    if re.search(r"</think>?", text, flags=re.IGNORECASE):
        text = re.split(r"</think>?", text, flags=re.IGNORECASE)[-1]
    text = re.split(r"<tool_call>|<tool_response>|<\|im_start\|>user", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"</?think>?", "", text, flags=re.IGNORECASE)
    return _compact(text, 4000)


def _nova_response_events(task_dir: Path, response_relative: Any, timestamp: float) -> list[dict[str, Any]]:
    """Project one durable Nova response into the Studio activity event schema.

    Only normalized assistant text and normalized tool calls are exposed.  Raw
    provider payloads, tool results, internal traces, request IDs and hidden
    reasoning are deliberately excluded from the monitor.
    """
    relative = Path(str(response_relative or ""))
    try:
        response_path = (task_dir / relative).resolve()
        task_root = task_dir.resolve()
    except OSError:
        return []
    if response_path != task_root and task_root not in response_path.parents:
        return []
    response = _json(response_path, {})
    content = response.get("content") if isinstance(response, dict) else None
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    base_sequence = int(max(0.0, timestamp) * 1_000_000)
    for offset, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        sequence = base_sequence + offset
        if kind == "text":
            text = _visible_nova_text(block.get("text"))
            if text:
                events.append({"k": "text", "s": text, "seq": sequence})
        elif kind == "tool_use":
            name = str(block.get("name") or "tool")
            events.append({
                "k": "tool",
                "tool": name,
                "hint": _nova_tool_hint(name, block.get("input")),
                "seq": sequence,
            })
    return events


def _nova_live_events(
    run: Path | None,
    sample: Path,
    config: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read incrementally persisted Nova attempts for every Agent in a sample.

    Nova's ``attempts.jsonl`` is durable after every model turn and remains the
    exact provider-level source for Nova runs.  The generic Harness now also
    refreshes ``messages.json`` atomically after every model/tool turn, which is
    used when Nova raw recording is not active.
    """
    if run is None:
        return {}
    config = config or {}
    sample_id = str(config.get("sample_id") or sample.name)
    task_roots = [run / "nova_raw" / "tasks"]
    batch = sample.parent.name
    task_roots.extend(root / batch / "tasks" for root in configured_nova_raw_roots())
    task_roots = [root for index, root in enumerate(task_roots) if root.is_dir() and root not in task_roots[:index]]
    if not task_roots:
        return {}
    feeds: dict[str, list[dict[str, Any]]] = {}
    for task_root in task_roots:
        for task_dir in sorted(task_root.iterdir()):
            if not task_dir.is_dir():
                continue
            meta = _json(task_dir / "task_meta.json", {})
            if not isinstance(meta, dict) or str(meta.get("sample_id") or "") != sample_id:
                continue
            label = str(meta.get("label") or "").strip() or task_dir.name
            key = "orch" if str(meta.get("role") or "").lower() == "orchestrator" or label in {"orch", "orchestrator"} else label
            attempts_path = task_dir / "attempts.jsonl"
            if not attempts_path.is_file():
                feeds.setdefault(key, [])
                continue
            events: list[dict[str, Any]] = []
            try:
                lines = attempts_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for raw in lines:
                try:
                    attempt = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(attempt, dict):
                    continue
                if attempt.get("selected") is False or str(attempt.get("status") or "").lower() != "ok":
                    continue
                try:
                    timestamp = float(attempt.get("recorded_at_epoch") or 0.0)
                except (TypeError, ValueError):
                    timestamp = 0.0
                events.extend(_nova_response_events(task_dir, attempt.get("response"), timestamp))
            if events:
                # A retry may persist the same normalized call twice. Keep its
                # first real occurrence while preserving cross-Agent timestamps.
                deduped: list[dict[str, Any]] = []
                previous: tuple[Any, ...] | None = None
                for event in sorted(events, key=lambda item: int(item.get("seq") or 0)):
                    identity = (event.get("k"), event.get("tool"), event.get("hint"), event.get("s"))
                    if identity == previous:
                        continue
                    previous = identity
                    deduped.append(event)
                feeds[key] = deduped
    return feeds


def _overall_timing(
    sample: Path,
    agents: list[dict[str, Any]],
    *,
    run: Path | None = None,
    config: dict[str, Any] | None = None,
    complete: bool = False,
    status: str | None = None,
) -> dict[str, Any]:
    config = config or _json(sample / "_trace/orchestrator/config.json", {}) or {}
    config_path = sample / "_trace/orchestrator/config.json"
    started_ts = (
        _timestamp(config.get("authoritative_task_started_at"))
        or _timestamp(config.get("started_at"))
        or (config_path.stat().st_mtime if config_path.is_file() else None)
    )
    effective_status = status or ("completed" if complete else "running")
    terminal = effective_status != "running"
    finished_ts = None
    if terminal and run is not None:
        sample_id = str(config.get("sample_id") or sample.name)
        finished_ts = _timestamp(_manifest_record(run, sample_id).get("finished_at"))
    if terminal and finished_ts is None:
        finished_ts = max(
            (_timestamp(agent.get("finished_at")) or 0.0 for agent in agents),
            default=0.0,
        ) or None
    if terminal and finished_ts is None:
        finished_ts = _latest_mtime(sample)
    timing_end = finished_ts or (time.time() if started_ts and not terminal else None)
    return {
        "started_at": _iso(started_ts),
        "finished_at": _iso(finished_ts),
        "duration_s": round(max(0.0, timing_end - started_ts), 1) if started_ts and timing_end else None,
        "status": effective_status,
    }


def _page_rows(run: Path, sample: Path) -> list[dict[str, Any]]:
    numbers: set[int] = set()
    for directory, suffix in ((sample / "slides", ".html"), (sample / "renders", ".png")):
        for path in directory.glob(f"slide_*{suffix}") if directory.is_dir() else ():
            match = re.search(r"slide_(\d+)", path.stem)
            if match:
                numbers.add(int(match.group(1)))
    run_id = quote(_run_id(run))
    sample_id = quote(str((_json(sample / "_trace/orchestrator/config.json", {}) or {}).get("sample_id") or sample.name))
    base = f"/api/trajectory-monitor/runs/{run_id}/samples/{sample_id}/files"
    pages: list[dict[str, Any]] = []
    for number in sorted(numbers):
        html = sample / "slides" / f"slide_{number:02d}.html"
        render = sample / "renders" / f"slide_{number:02d}.png"
        title = f"第 {number:02d} 页"
        if html.is_file():
            try:
                match = TITLE.search(html.read_text(encoding="utf-8", errors="replace")[:32_000])
                if match:
                    title = _compact(match.group(1), 120)
            except OSError:
                pass
        pages.append({
            "number": number,
            "title": title,
            "html_url": f"{base}/slides/slide_{number:02d}.html" if html.is_file() else None,
            "render_url": f"{base}/renders/slide_{number:02d}.png" if render.is_file() else None,
        })
    return pages


def _page_agents(sample: Path) -> dict[str, str]:
    owners: dict[str, str] = {}
    for config_path in (sample / "_trace/subagents").glob("slide*/config.json"):
        config = _json(config_path, {})
        key = str(config.get("label") or config_path.parent.name)
        handoff = _json(config_path.parent / "handoff.json", {})
        contract = handoff.get("contract", {}) if isinstance(handoff, dict) else {}
        raw = str(contract.get("pages") or "") if isinstance(contract, dict) else ""
        for number in re.findall(r"\d+", raw):
            owners[str(int(number))] = key
    return owners


def _feed_payload(
    sample: Path,
    complete: bool,
    *,
    run: Path | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    feed: dict[str, list[dict[str, Any]]] = {}
    timings: dict[str, dict[str, Any]] = {}
    agents = _agent_rows(sample, complete)
    for agent in agents:
        key = "orch" if agent["role"] == "orchestrator" or agent["label"] in {"orch", "orchestrator"} else agent["label"]
        events: list[dict[str, Any]] = []
        for seq, event in enumerate(agent.get("events") or []):
            if event.get("kind") == "tool":
                events.append({"k": "tool", "tool": event.get("name") or "tool", "hint": event.get("hint") or "", "seq": seq})
            else:
                events.append({"k": "text", "s": event.get("text") or "", "seq": seq})
        summary = str(agent.get("summary") or "").strip()
        if summary and (not events or events[-1].get("s") != summary):
            events.append({"k": "text", "s": summary, "seq": len(events)})
        feed[key] = events
        timings[key] = {
            "status": agent.get("status"),
            "started_at": agent.get("started_at"),
            "finished_at": agent.get("finished_at"),
            "duration_s": agent.get("duration_s"),
            "model_wall_seconds": agent.get("model_wall_seconds"),
        }
    # Prefer Nova's exact incremental responses whenever present; non-Nova and
    # historical runs use the durable Harness messages projected above.
    for key, events in _nova_live_events(run, sample, config).items():
        if events:
            feed[key] = events
    return {
        "agents": feed,
        "agent_timings": timings,
        "overall_timing": _overall_timing(
            sample,
            agents,
            run=run,
            config=config,
            complete=complete,
            status=status,
        ),
        "page_agents": _page_agents(sample),
        "specialist_artifacts": trace.specialist_artifacts(str(sample)),
    }


def _deck_title(sample: Path, config: dict[str, Any]) -> str:
    first = sample / "slides/slide_01.html"
    if first.is_file():
        try:
            match = TITLE.search(first.read_text(encoding="utf-8", errors="replace")[:32_000])
            if match:
                return _compact(match.group(1), 80)
        except OSError:
            pass
    return _compact(config.get("task") or config.get("sample_id") or sample.name, 80)


def decks_payload() -> dict[str, Any]:
    manifests: dict[Path, dict[str, str]] = {}
    decks: list[dict[str, Any]] = []
    for record in _catalog_records():
        run = record["run"]
        sample = record["sample"]
        config = record["config"]
        if run is None or sample is None:
            status = "not_started"
            updated_at = ""
        else:
            run_manifests = manifests.setdefault(run, _manifest_statuses(run))
            sample_id = str(config.get("sample_id") or sample.name)
            summary = _sample_summary(sample, config, run_manifests.get(sample_id), run=run)
            status = summary["status"]
            updated_at = summary["updated_at"] or record["updated_at"] or ""
        decks.append({
            "id": record["id"],
            "title": record["query"],
            "status": status,
            "presentation_kind": "static",
            "created_at": updated_at,
            "updated_at": updated_at,
            "pinned": False,
        })
    status_rank = {"completed": 3, "running": 2, "queued": 2, "waiting": 2, "not_started": 0}
    decks.sort(
        key=lambda deck: (status_rank.get(str(deck["status"]), 1), str(deck["updated_at"] or "")),
        reverse=True,
    )
    return {"decks": decks}


def deck_progress_payload(
    run: Path,
    sample: Path,
    config: dict[str, Any],
    *,
    deck_id: int | None = None,
) -> dict[str, Any]:
    sample_id = str(config.get("sample_id") or sample.name)
    summary = _sample_summary(sample, config, _manifest_statuses(run).get(sample_id), run=run)
    pages = _page_rows(run, sample)
    rendered = [page["number"] for page in pages if page["render_url"]]
    authored = [page["number"] for page in pages if page["html_url"]]
    stage_phase = {
        "starting": "starting", "planning": "planning", "assets": "designing",
        "slides": "delegating", "review": "verifying", "completed": "done",
        "rejected": "failed", "failed": "failed", "error": "failed", "blocked": "failed",
    }
    rtimes = {
        str(number): int((sample / "renders" / f"slide_{number:02d}.png").stat().st_mtime)
        for number in rendered
    }
    agents = _agent_rows(sample, summary["status"] == "completed")
    overall_timing = _overall_timing(
        sample,
        agents,
        run=run,
        config=config,
        complete=summary["status"] == "completed",
        status=summary["status"],
    )
    current = next((agent for agent in reversed(agents) if agent["status"] == "running"), None)
    latest_tool = None
    nova_feeds = _nova_live_events(run, sample, config)
    if current:
        latest_tool = next((event for event in reversed(current.get("events") or []) if event.get("kind") == "tool"), None)
        current_key = "orch" if current["role"] == "orchestrator" or current["label"] in {"orch", "orchestrator"} else current["label"]
        live_tool = next((event for event in reversed(nova_feeds.get(current_key) or []) if event.get("k") == "tool"), None)
        if live_tool:
            latest_tool = {"name": live_tool.get("tool")}
    phase = stage_phase.get(summary["stage"], "starting")
    if summary["status"] == "running" and current:
        label = str(current.get("label") or "").lower()
        if label.startswith("research"):
            phase = "researching"
        elif label.startswith(("material", "image")):
            phase = "designing"
        elif label.startswith("slide"):
            phase = "delegating"
        elif label.startswith("review"):
            phase = "verifying"
    return {
        "deck_id": deck_id if deck_id is not None else _deck_id(run, sample),
        "title": _deck_title(sample, config),
        "status": summary["status"],
        "phase": phase,
        "rendered": rendered,
        "slides_total": max(summary["expected_pages"], len(pages)),
        "slides_rendered": len(rendered),
        "slides_authored": len(authored),
        "rtimes": rtimes,
        "html_ready": bool(pages),
        "html_final": (sample / "present.html").is_file(),
        "html_stamp": int(_latest_mtime(sample)),
        "html_entry": (
            "present.html" if (sample / "present.html").is_file()
            else (f"slides/slide_{authored[0]:02d}.html" if authored else "present.html")
        ),
        "ppt_output": "static_html",
        "query": str(config.get("task") or ""),
        "user_query": str(config.get("task") or ""),
        "model": str(config.get("model") or ""),
        "pipeline": "mural-presenter",
        "skill_version": "mural-presenter",
        "slide_count": summary["expected_pages"] or len(pages),
        "thinking": _thinking_enabled(config),
        "runtime_limits": {},
        "started_at": overall_timing["started_at"],
        "finished_at": overall_timing["finished_at"],
        "elapsed_s": overall_timing["duration_s"],
        "revision_no": 0,
        "revision_supported": False,
        "conversation_turns": [{"role": "user", "content": str(config.get("task") or "")}, {"role": "assistant", "current": True, "status": summary["status"]}],
        "activity": ({"agent": current["label"], "tool": latest_tool["name"]} if current and latest_tool else None),
    }


def not_started_progress_payload(record: dict[str, Any]) -> dict[str, Any]:
    config = record["config"]
    page_count = int((config.get("generation_preferences") or {}).get("page_count") or 0)
    query = str(record["query"])
    return {
        "deck_id": record["id"],
        "title": query,
        "status": "not_started",
        "phase": "not_started",
        "rendered": [],
        "slides_total": page_count,
        "slides_rendered": 0,
        "slides_authored": 0,
        "rtimes": {},
        "html_ready": False,
        "html_final": False,
        "html_stamp": 0,
        "html_entry": None,
        "ppt_output": "static_html",
        "query": query,
        "user_query": query,
        "model": str(os.environ.get("STUDIO_TRAJECTORY_DEFAULT_MODEL") or ""),
        "pipeline": "mural-presenter",
        "skill_version": "mural-presenter",
        "slide_count": page_count,
        "thinking": True,
        "runtime_limits": {},
        "revision_no": 0,
        "revision_supported": False,
        "conversation_turns": [
            {"role": "user", "content": query},
            {"role": "assistant", "current": True, "status": "not_started"},
        ],
        "activity": None,
    }


def _sample_summary(
    sample: Path,
    config: dict[str, Any],
    manifest_status: str | None = None,
    *,
    run: Path | None = None,
) -> dict[str, Any]:
    sample_id = str(config.get("sample_id") or sample.name)
    expected = int((config.get("generation_preferences") or {}).get("page_count") or 0)
    plans = len(list((sample / "plan").glob("slide_*.md")))
    slides = len(list((sample / "slides").glob("slide_*.html")))
    renders = len(list((sample / "renders").glob("slide_*.png")))
    assets = _ready_asset_count(sample)
    present = (sample / "present.html").is_file()
    review_started = (sample / "_trace/subagents/review").is_dir()
    slide_started = any((sample / "_trace/subagents").glob("slide*/config.json"))
    image_started = any((sample / "_trace/subagents").glob("image*/config.json"))
    terminal_status = str(manifest_status or "").lower()
    if present or terminal_status == "completed":
        stage, progress, status = "completed", 100, "completed"
    elif terminal_status in {"rejected", "error", "failed", "blocked"}:
        stage, progress, status = terminal_status, 100, terminal_status
    elif review_started:
        stage, progress, status = "review", 88, "running"
    elif slide_started or slides or renders:
        ratio = min(1.0, max(slides, renders) / max(1, expected or plans or 1))
        stage, progress, status = "slides", round(45 + 38 * ratio), "running"
    elif image_started or assets:
        stage, progress, status = "assets", 38, "running"
    elif plans:
        ratio = min(1.0, plans / max(1, expected or plans))
        stage, progress, status = "planning", round(12 + 18 * ratio), "running"
    else:
        stage, progress, status = "starting", 5, "running"
    if status == "running" and not _sample_process_alive(run, sample):
        status = "stopped"
    return {
        "sample_id": sample_id,
        "batch": sample.parent.name,
        "task": str(config.get("task") or ""),
        "model": str(config.get("model") or ""),
        "stage": stage,
        "status": status,
        "progress": progress,
        "expected_pages": expected,
        "plan_pages": plans,
        "slide_pages": slides,
        "render_pages": renders,
        "ready_assets": assets,
        "updated_at": _iso(_latest_mtime(sample)),
    }


def _tail_log(path: Path, sample_id: str, lines: int = 120, max_bytes: int = 512_000) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            text = stream.read().decode("utf-8", "replace")
    except OSError:
        return []
    selected = [ANSI.sub("", line) for line in text.splitlines() if not sample_id or sample_id in line]
    return selected[-lines:]


def _artifact_rows(run: Path, sample: Path) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    candidates.extend(sorted((sample / "renders").glob("slide_*.png")))
    candidates.extend(sorted((sample / "assets").glob("contact-sheet*.png")))
    candidates.extend(path for path in (sample / "assets").glob("*.png") if "_work" not in path.name)
    for relative in ("present.html", "speech.md", "plan/deck.md", "plan/design-brief.md"):
        path = sample / relative
        if path.is_file():
            candidates.append(path)
    deduped: dict[str, Path] = {}
    for path in candidates:
        deduped[str(path.resolve())] = path
    rows: list[dict[str, Any]] = []
    for path in sorted(deduped.values(), key=lambda item: item.stat().st_mtime, reverse=True)[:80]:
        relative = path.resolve().relative_to(run.resolve()).as_posix()
        rows.append({
            "name": path.name,
            "path": relative,
            "kind": "image" if path.suffix.lower() in IMAGE_SUFFIXES else "document",
            "size": path.stat().st_size,
            "updated_at": _iso(path.stat().st_mtime),
            "url": f"/api/trajectory-monitor/runs/{quote(_run_id(run))}/artifacts/{quote(relative, safe='/')}",
        })
    return rows


def runs_payload() -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for run in _run_directories():
        samples = _sample_roots(run)
        pid = _pid(run / "launcher.pid")
        runs.append({
            "run_id": _run_id(run),
            "name": run.name,
            "path": str(run),
            "running": _pid_alive(pid),
            "pid": pid,
            "sample_count": len(samples),
            "updated_at": _iso(_latest_mtime(run)),
        })
    return {"roots": [str(path) for path in configured_roots()], "runs": runs}


def run_payload(run: Path, *, offset: int = 0, limit: int = 200, search: str = "") -> dict[str, Any]:
    manifests = _manifest_statuses(run)
    summaries = [
        _sample_summary(
            sample,
            config,
            manifests.get(str(config.get("sample_id") or sample.name)),
            run=run,
        )
        for sample, config in _sample_roots(run)
    ]
    if search:
        needle = search.casefold()
        summaries = [row for row in summaries if needle in (row["sample_id"] + " " + row["task"]).casefold()]
    counts = Counter(row["stage"] for row in summaries)
    return {
        "run_id": _run_id(run),
        "name": run.name,
        "path": str(run),
        "running": _pid_alive(_pid(run / "launcher.pid")),
        "updated_at": _iso(_latest_mtime(run)),
        "total": len(summaries),
        "stage_counts": dict(counts),
        "offset": offset,
        "limit": limit,
        "samples": summaries[offset: offset + limit],
    }


def sample_payload(run: Path, sample_id: str) -> dict[str, Any]:
    manifests = _manifest_statuses(run)
    for sample, config in _sample_roots(run):
        resolved_id = str(config.get("sample_id") or sample.name)
        if resolved_id != sample_id:
            continue
        summary = _sample_summary(sample, config, manifests.get(resolved_id), run=run)
        complete = summary["status"] == "completed"
        return {
            **summary,
            "workspace": str(sample),
            "agents": _agent_rows(sample, complete),
            "artifacts": _artifact_rows(run, sample),
            "recent_log": _tail_log(run / "run.log", resolved_id),
            "pages": _page_rows(run, sample),
            "presentation_url": (
                f"/api/trajectory-monitor/runs/{quote(_run_id(run))}/samples/{quote(resolved_id)}/files/present.html"
                if (sample / "present.html").is_file() else None
            ),
        }
    raise HTTPException(status_code=404, detail="trajectory sample not found")


def _safe_artifact(run: Path, relative: str) -> Path:
    target = (run / relative).resolve()
    root = run.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="artifact path escapes trajectory run")
    if not target.is_file() or target.suffix.lower() not in IMAGE_SUFFIXES | TEXT_SUFFIXES:
        raise HTTPException(status_code=404, detail="artifact not found")
    return target


def _resolve_sample(run: Path, sample_id: str) -> Path:
    for sample, config in _sample_roots(run):
        if str(config.get("sample_id") or sample.name) == sample_id:
            return sample
    raise HTTPException(status_code=404, detail="trajectory sample not found")


def _safe_sample_file(sample: Path, relative: str) -> Path:
    target = (sample / relative).resolve()
    root = sample.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="sample file path escapes workspace")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="sample file not found")
    return target


@router.get("/trajectory-monitor", response_class=HTMLResponse)
def trajectory_monitor_page(request: Request):
    return templates.TemplateResponse(request, "app.html", {
        "user": {"id": 0, "username": "轨迹查看", "display_name": "轨迹查看", "role": "viewer"},
        "authenticated": True,
        "conversations": [],
        "edition": "trajectory",
        "auth_enabled": False,
        "dynamic_enabled": False,
        "models": [],
        "default_model": "",
        "pipelines": [],
        "default_pipeline": "",
        "skills": [],
        "default_skill": "",
        "generation_stack": {},
        "trajectory_mode": True,
    })


@router.get("/api/trajectory-monitor/decks")
def trajectory_decks():
    return decks_payload()


@router.get("/api/trajectory-monitor/decks/{deck_id}/progress")
def trajectory_deck_progress(deck_id: int):
    record = _resolve_catalog_deck(deck_id)
    if record["run"] is None or record["sample"] is None:
        return not_started_progress_payload(record)
    return deck_progress_payload(record["run"], record["sample"], record["config"], deck_id=deck_id)


@router.get("/api/trajectory-monitor/decks/{deck_id}/livefeed")
def trajectory_deck_livefeed(deck_id: int):
    record = _resolve_catalog_deck(deck_id)
    if record["run"] is None or record["sample"] is None:
        return {"agents": {}, "page_agents": {}, "specialist_artifacts": {"agents": {}}}
    run, sample, config = record["run"], record["sample"], record["config"]
    summary = _sample_summary(
        sample,
        config,
        _manifest_statuses(run).get(str(config.get("sample_id") or sample.name)),
        run=run,
    )
    return _feed_payload(
        sample,
        summary["status"] == "completed",
        run=run,
        config=config,
        status=summary["status"],
    )


@router.get("/api/trajectory-monitor/decks/{deck_id}/file")
def trajectory_deck_file(deck_id: int, rel: str):
    _, sample, _ = _resolve_deck(deck_id)
    return FileResponse(_safe_sample_file(sample, rel))


@router.get("/api/trajectory-monitor/decks/{deck_id}/files/{relative:path}")
def trajectory_deck_file_path(deck_id: int, relative: str):
    _, sample, _ = _resolve_deck(deck_id)
    return FileResponse(_safe_sample_file(sample, relative))


@router.get("/api/trajectory-monitor/decks/{deck_id}/slideinfo")
def trajectory_deck_slideinfo(deck_id: int, n: int = Query(..., ge=0)):
    record = _resolve_catalog_deck(deck_id)
    if record["sample"] is None:
        return {"exists": False}
    sample = record["sample"]
    relative = "plan/deck.md" if n == 0 else f"plan/slide_{n:02d}.md"
    path = sample / relative
    if not path.is_file():
        return {"exists": False}
    html_rel = "base.css" if n == 0 else f"slides/slide_{n:02d}.html"
    return {
        "exists": True,
        "html": mdrender.render(path.read_text(encoding="utf-8", errors="replace")),
        "html_rel": html_rel if (sample / html_rel).is_file() else None,
        "assets": [],
    }


@router.get("/api/trajectory-monitor/decks/{deck_id}/page-history")
def trajectory_deck_page_history(deck_id: int, n: int = Query(..., ge=1)):
    record = _resolve_catalog_deck(deck_id)
    if record["sample"] is None:
        return {"page": n, "items": [], "speech": {"exists": False, "page": n}}
    sample = record["sample"]
    data = trace.page_history(str(sample), n, max_events=100)
    data["speech"] = trace.page_speech(str(sample), n)
    for item in data.get("items", []):
        relative = str(item.get("image_url") or "")
        item["image_url"] = (
            f"/api/trajectory-monitor/decks/{deck_id}/file?rel={quote(relative)}"
            if relative else ""
        )
    return data


@router.get("/api/trajectory-monitor/runs")
def trajectory_runs():
    return runs_payload()


@router.get("/api/trajectory-monitor/runs/{run_id}")
def trajectory_run(
    run_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    search: str = Query("", max_length=200),
):
    return run_payload(_resolve_run(run_id), offset=offset, limit=limit, search=search.strip())


@router.get("/api/trajectory-monitor/runs/{run_id}/samples/{sample_id}")
def trajectory_sample(run_id: str, sample_id: str):
    return sample_payload(_resolve_run(run_id), sample_id)


@router.get("/api/trajectory-monitor/runs/{run_id}/artifacts/{relative:path}")
def trajectory_artifact(run_id: str, relative: str):
    return FileResponse(_safe_artifact(_resolve_run(run_id), relative))


@router.get("/api/trajectory-monitor/runs/{run_id}/samples/{sample_id}/files/{relative:path}")
def trajectory_sample_file(run_id: str, sample_id: str, relative: str):
    run = _resolve_run(run_id)
    return FileResponse(_safe_sample_file(_resolve_sample(run, sample_id), relative))
