"""Turn the engine's on-disk run_dir into a friendly progress snapshot.

The engine writes plan/ slides/ renders/ and each Agent's messages/tool log in
real time.  The runner subprocess also streams compact activity lines to
data/jobs/<id>.log.  Filesystem artifacts drive phase/page progress; the durable
per-Agent trace replaces the compact log preview as soon as one model response
has landed, so the UI can show complete in-progress prose without waiting for
the role to stop.
"""
import glob
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

# runner log lines look like:  [u2_d9/slide_03] 🔧 read({"path": ...})
_ACT_RE = re.compile(r"\[[^/\]]+/([^\]]+)\] 🔧 (\w+)\(")

# Image agents sometimes copy a generated hash-named image to a readable
# semantic filename.  Both paths are useful to the deck, but the activity
# gallery should show the visual only once.  Cache content hashes by immutable
# stat identity so polling livefeed does not repeatedly read large PNG files.
_ASSET_DIGEST_CACHE = {}


def _asset_digest(path, stat):
    key = (path, stat.st_mtime_ns, stat.st_size)
    digest = _ASSET_DIGEST_CACHE.get(key)
    if digest:
        return digest
    sha = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    if len(_ASSET_DIGEST_CACHE) >= 4096:
        _ASSET_DIGEST_CACHE.clear()
    _ASSET_DIGEST_CACHE[key] = digest
    return digest


def _slide_nums(pattern):
    nums = []
    for p in glob.glob(pattern):
        b = os.path.basename(p)
        try:
            nums.append(int(b.split("_")[1].split(".")[0]))
        except (IndexError, ValueError):
            pass
    return sorted(set(nums))


def _last_activity(log_path):
    """Last tool call from the runner log: {'agent': 'slide_03', 'tool': 'read'}."""
    if not log_path or not os.path.exists(log_path):
        return None
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - 8192))
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    m = None
    for m in _ACT_RE.finditer(chunk):
        pass
    return {"agent": _canonical_agent_key(m.group(1)), "tool": m.group(2)} if m else None


def snapshot(run_dir, seed=None, status=None, started_at=None, finished_at=None, log_path=None):
    rd = run_dir
    task_pack = {}
    try:
        with open(os.path.join(rd, "task_pack.json"), encoding="utf-8") as source:
            task_pack = json.load(source)
    except (OSError, ValueError, TypeError):
        pass
    output = (task_pack.get("choices") or {}).get("output") or (seed or {}).get("ppt_output")
    authored = _slide_nums(os.path.join(rd, "slides", "slide_*.html"))
    rendered = _slide_nums(os.path.join(rd, "renders", "slide_*.png"))
    if output == "dynamic_html":
        rendered = _slide_nums(os.path.join(rd, "shots", "page_*.png"))
        if not rendered:
            rendered = _slide_nums(os.path.join(rd, "shots", "slide_*.png"))
        authored = list(rendered)
    # 每页渲染文件的毫秒级 mtime：页面被自检快速重渲时，
    # 秒级时间戳会把同一秒内的真实修改误判为未变。
    rtimes = {}
    for n in rendered:
        try:
            image_path = (
                os.path.join(rd, "shots", f"page_{n:02d}.png")
                if output == "dynamic_html"
                else os.path.join(rd, "renders", f"slide_{n:02d}.png")
            )
            stat = os.stat(image_path)
            rtimes[str(n)] = stat.st_mtime_ns // 1_000_000
        except OSError:
            pass
    workers = len(glob.glob(os.path.join(rd, "_trace", "subagents", "slide_*")))

    total = len(glob.glob(os.path.join(rd, "plan", "slide_*.md")))
    if task_pack:
        try:
            total = int((task_pack.get("params") or {}).get("page_count") or total)
        except (TypeError, ValueError):
            pass
    if not total and seed and seed.get("slide_count"):
        try:
            total = int(seed["slide_count"])
        except (TypeError, ValueError):
            total = 0

    prog = {
        "status": status,
        "slides_total": total or None,
        "slides_authored": len(authored),
        "slides_rendered": len(rendered),
        "rendered": rendered,
        "rtimes": rtimes,
        "workers": workers,
        "activity": _last_activity(log_path),
        "elapsed_s": ((finished_at or int(time.time())) - started_at) if started_at else None,
        "error": None,
        "ppt_output": output or "static_html",
        "task_stage": (task_pack.get("state") or {}).get("current_stage"),
        "completed_stages": (task_pack.get("state") or {}).get("completed_stages") or [],
    }
    prog["phase"] = _phase(prog, rd, status, task_pack=task_pack)
    return prog


def _phase(prog, rd, status, task_pack=None):
    if status == "completed":
        return "done"
    if status in ("failed", "rejected", "interrupted", "stopped"):
        return "failed"
    if status == "queued" or not os.path.isdir(os.path.join(rd, "_trace")):
        return "starting"
    stage = str(((task_pack or {}).get("state") or {}).get("current_stage") or "")
    if stage:
        if stage == "entry" or "research" in stage:
            return "researching"
        if stage == "story":
            return "planning"
        if stage.endswith(".plan"):
            return "designing"
        if any(token in stage for token in (".pages", ".build", ".content")):
            return "rendering"
        if any(token in stage for token in (".review", ".verify", ".final", ".deliver")):
            return "verifying"
    planned = len(glob.glob(os.path.join(rd, "plan", "slide_*.md")))
    if planned and prog["slides_rendered"] >= planned:
        return "verifying"
    if prog["workers"] or prog["slides_authored"] or prog["slides_rendered"]:
        return "rendering"
    if planned:
        return "delegating"
    act = prog.get("activity") or {}
    tool = act.get("tool")
    if tool in ("web_search", "web_fetch"):
        return "researching"
    if tool == "image_generate" or glob.glob(os.path.join(rd, "assets", "*")) \
       or os.path.exists(os.path.join(rd, "base.css")):
        return "designing"
    return "planning"


# ---- 实时输出流:解析 runner 日志,按 agent 分组 ----------------------------
# 旧格式:[u6_d14/slide_04_r2] 🔧 edit({"path": "slides/slide_04.html", ...
# 新格式:[09:01:28 + 706.0s] [u6_d14/orchestrator] [3] 💬 我先读取 skill 说明…
# 时间前缀由当前 clean Harness 输出；保持可选以兼容历史 deck 日志。
_TIMING_PREFIX_RE = r"(?:\[\d{2}:\d{2}:\d{2} \+\s*\d+(?:\.\d+)?s\]\s+)?"
_FEED_RE = re.compile(
    _TIMING_PREFIX_RE
    + r"\[[^/\]]+/([^\]]+)\] (?:🔧 (\w+)\((.*)|\[\d+\] 💬 (.*))"
)
_HINT_RE = re.compile(r'"path"\s*:\s*"([^"]+)"')
_PLAN_GROUP_RE = re.compile(
    r"^\s*-\s*production_group\s*[:：]\s*[`\"']?([A-Za-z0-9._-]+)", re.I | re.M,
)
_SLIDE_REF_RE = re.compile(r"(?:slide_|page_)0*(\d{1,3})(?=\D|$)", re.I)
_PAGES_ARG_RE = re.compile(r"--pages(?:=|[ \t]+)([0-9][0-9, \t]*)", re.I)


def _agent_alias_from_task(task, fallback):
    """Recover a stable role label when a Harness trace was named child_NN."""
    text = str(task or "").strip()
    # Current grouped Skills include a durable assignment line such as
    # ``Slide Group roots [27,28]``.  Prefer it over the runner label: labels
    # have appeared as slide-group-roots, slide-roots and slide-g08 while the
    # production-group id in the plan remains stable.
    explicit_groups = re.findall(
        r"\bSlide\s+Group\s+([A-Za-z0-9._-]+)\s*\[\s*\d",
        text,
        re.I,
    )
    if explicit_groups:
        return f"slide_group_{explicit_groups[-1].lower()}"
    identity_group = re.search(
        r"(?:身份是|identity\s*(?:is|:))\s*slide[-_]group[-_]([A-Za-z0-9._-]+)",
        text,
        re.I,
    )
    if identity_group:
        return f"slide_group_{identity_group.group(1).lower()}"
    bracketed = re.search(
        r"\[\s*(research|material|image|slide|review)(?:[\s_-]+([^\]]+))?\s*\]",
        text,
        re.I,
    )
    if bracketed:
        role = bracketed.group(1).lower()
        suffix = re.sub(r"[^a-zA-Z0-9._-]+", "-", bracketed.group(2) or "").strip("-_").lower()
        if role == "slide":
            if suffix.isdigit():
                return f"slide_{int(suffix):02d}"
            return f"slide_group_{suffix or 'group'}"
        if role == "review":
            return "review"
        return f"{role}_{suffix}" if suffix else role
    group = re.match(r"slide\s+group\s+([^\s:\[]+)", text, re.I)
    if group:
        suffix = re.sub(r"[^a-zA-Z0-9._-]+", "-", group.group(1)).strip("-_").lower()
        return f"slide_group_{suffix or 'group'}"
    single = re.match(r"slide\s+0*(\d{1,3})\b", text, re.I)
    if single:
        return f"slide_{int(single.group(1)):02d}"
    for role in ("research", "material", "image", "review"):
        if re.match(rf"(?:final\s+)?{role}\b", text, re.I):
            return role
    return str(fallback or "")


def _trace_agent_aliases(run_dir):
    aliases = {}
    trace_root = os.path.join(run_dir or "", "_trace", "subagents")
    for config_path in glob.glob(os.path.join(trace_root, "*", "config.json")):
        raw = os.path.basename(os.path.dirname(config_path))
        try:
            with open(config_path, encoding="utf-8", errors="replace") as source:
                config = json.load(source)
        except (OSError, ValueError, TypeError):
            continue
        aliases[raw] = _agent_alias_from_task(
            config.get("task") or config.get("goal") or "",
            config.get("label") or raw,
        )
    return aliases


def _json_text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _json_text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_text_values(item)


def _plan_page_agents(run_dir):
    """Read the grouped Skill's stable page ownership from plan/slide_NN.md."""
    mapping = {}
    if not run_dir:
        return mapping
    for path in glob.glob(os.path.join(run_dir, "plan", "slide_*.md")):
        try:
            page = int(os.path.basename(path).split("_")[1].split(".")[0])
            with open(path, encoding="utf-8", errors="replace") as source:
                text = source.read(8192)
        except (OSError, IndexError, ValueError):
            continue
        match = _PLAN_GROUP_RE.search(text)
        if match:
            mapping[str(page)] = f"slide_group_{match.group(1)}"
    return mapping


def _group_event_pages(args):
    """Best-effort page references from a grouped Agent tool call."""
    pages = {int(value) for value in _SLIDE_REF_RE.findall(args or "")}
    for match in _PAGES_ARG_RE.finditer(args or ""):
        pages.update(int(value) for value in re.findall(r"\d+", match.group(1)))
    return {page for page in pages if page > 0}


def _canonical_agent_key(value):
    """Normalize historical public aliases without renaming trace directories."""
    raw = str(value or "").strip()
    base = re.sub(r"_r\d+$", "", raw, flags=re.I)
    if base.lower() in {"orch", "orchestrator"}:
        return "orch"
    return base


def _livefeed_agent_key(raw_agent, aliases=None):
    """Normalize retry Agent labels to the stable key consumed by the UI."""
    raw_agent = str(raw_agent or "")
    base_agent = _canonical_agent_key(raw_agent)
    alias = (aliases or {}).get(raw_agent) or (aliases or {}).get(base_agent) or base_agent
    base_agent = _canonical_agent_key(alias)
    grouped = re.match(r"slide[-_]group[-_](.+)", base_agent, re.I)
    if grouped:
        return f"slide_group_{grouped.group(1)}"
    slide_match = re.match(r"(slide_\d+)", base_agent)
    return slide_match.group(1) if slide_match else base_agent


def _streaming_delta_events(role_dir, messages, response_language, sequence):
    """Project only the current, not-yet-canonical model turn into the UI.

    ``live-deltas.jsonl`` is deliberately not a training source.  Once the
    matching Assistant response appears in atomic ``messages.json``, this
    projection disappears and the canonical blocks take over.
    """
    path = os.path.join(role_dir, "live-deltas.jsonl")
    if not os.path.isfile(path):
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as source:
            start = max(0, size - 2 * 1024 * 1024)
            source.seek(start)
            raw = source.read().decode("utf-8", errors="ignore")
        lines = raw.splitlines()
        if start and lines:
            lines = lines[1:]
    except OSError:
        return []
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
        return []
    current = starts[-1]
    stream_id = str(current.get("stream_id") or "")
    canonical_assistant_count = sum(
        1 for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )
    try:
        base_assistant_count = int(current.get("base_assistant_count") or 0)
    except (TypeError, ValueError):
        base_assistant_count = 0
    if canonical_assistant_count > base_assistant_count:
        return []
    stream_records = [
        item for item in records
        if str(item.get("stream_id") or "") == stream_id
        and item.get("event") == "delta"
    ]
    visible_text = "".join(
        str(item.get("text") or "")
        for item in stream_records if item.get("kind") == "text"
    )
    visible_text = _visible_text_for_language(visible_text, response_language)
    events = []
    if visible_text:
        events.append({
            "k": "text", "s": visible_text, "seq": sequence,
            "partial": True,
        })
        sequence += 1
    seen_tools = set()
    for item in stream_records:
        if item.get("kind") != "tool_start":
            continue
        name = str(item.get("tool_name") or item.get("text") or "tool")
        tool_use_id = str(item.get("tool_use_id") or "")
        identity = tool_use_id or name
        if identity in seen_tools:
            continue
        seen_tools.add(identity)
        events.append({
            "k": "tool", "tool": name, "hint": "正在接收参数…",
            "seq": sequence, "partial": True,
        })
        sequence += 1
    return events


def _live_trace_events(run_dir, aliases, response_language, active_keys):
    """Read complete in-progress Assistant turns from atomic Harness traces.

    Only agents already present in the current runner log are eligible.  This
    prevents traces retained from an earlier static revision from appearing in
    the newest conversation turn.
    """
    if not run_dir:
        return {}
    role_dirs = [os.path.join(run_dir, "_trace", "orchestrator")]
    role_dirs.extend(sorted(glob.glob(os.path.join(run_dir, "_trace", "subagents", "*"))))
    result = {}
    for role_dir in role_dirs:
        if not os.path.isdir(role_dir):
            continue
        config = _read_trace_json(os.path.join(role_dir, "config.json"), {})
        raw_agent = str(config.get("label") or os.path.basename(role_dir))
        if str(config.get("role") or "").lower() == "orchestrator":
            raw_agent = "orch"
        key = _livefeed_agent_key(raw_agent, aliases)
        if key not in active_keys:
            continue
        messages = _read_trace_json(os.path.join(role_dir, "messages.json"), [])
        if not isinstance(messages, list):
            continue
        events = []
        sequence = 0
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else [
                {"type": "text", "text": content}
            ]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = str(block.get("type") or "")
                if kind == "text":
                    value = _visible_text_for_language(
                        str(block.get("text") or ""), response_language
                    )
                    if value:
                        events.append({"k": "text", "s": value, "seq": sequence})
                        sequence += 1
                elif kind == "tool_use":
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    hint = ""
                    for field in ("path", "query", "pattern", "command", "aspect_ratio", "url"):
                        if args.get(field):
                            hint = str(args[field])
                            break
                    events.append({
                        "k": "tool",
                        "tool": str(block.get("name") or "tool"),
                        "hint": hint,
                        "seq": sequence,
                    })
                    sequence += 1
        events.extend(
            _streaming_delta_events(
                role_dir, messages, response_language, sequence
            )
        )
        if events:
            result[key] = events
    return result


def _trace_timestamp(value):
    """Parse Harness timestamps without inventing a time for old traces."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace(" UTC", "+00:00")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Historical Harness versions persisted the host-local wall clock
        # without an offset.  Interpret those values in the same host-local
        # timezone instead of relabelling them as UTC.  New traces always carry
        # an explicit trailing Z and do not enter this compatibility branch.
        return time.mktime(parsed.timetuple()) + parsed.microsecond / 1_000_000
    return parsed.timestamp()


def _trace_iso(timestamp):
    if not timestamp:
        return None
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat().replace("+00:00", "Z")


def _trace_pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _agent_timing_payload(run_dir, aliases=None):
    """Return real per-Agent wall-clock boundaries persisted by the Harness.

    The regular WebDemo consumes runner logs through :func:`livefeed`, while
    the trajectory monitor already exposed these timestamps independently.
    Keeping the derivation here makes the primary UI receive the same timing
    data without changing the Harness or fabricating timestamps for old jobs.
    """
    if not run_dir:
        return {}, None
    configs = [os.path.join(run_dir, "_trace", "orchestrator", "config.json")]
    configs.extend(sorted(glob.glob(os.path.join(run_dir, "_trace", "subagents", "*", "config.json"))))
    timings = {}
    for config_path in configs:
        try:
            with open(config_path, encoding="utf-8", errors="replace") as source:
                config = json.load(source)
            config_stat = os.stat(config_path)
        except (OSError, ValueError, TypeError):
            continue
        role_dir = os.path.dirname(config_path)
        raw_label = str(config.get("label") or os.path.basename(role_dir))
        key = _livefeed_agent_key(raw_label, aliases)
        started = _trace_timestamp(config.get("started_at"))
        if started is None:
            # A config mtime is reliable evidence that the Agent existed. It is
            # only used for historical Harness versions that omitted started_at.
            started = config_stat.st_mtime
        completion_paths = [
            os.path.join(role_dir, name)
            for name in ("messages.json", "tool_log.json", "summary.md", "usage.json", "handoff.json")
            if os.path.isfile(os.path.join(role_dir, name))
        ]
        complete = any(os.path.basename(path) in {"summary.md", "usage.json", "handoff.json"} for path in completion_paths)
        running = not complete and _trace_pid_alive(config.get("pid"))
        finished = max((os.path.getmtime(path) for path in completion_paths), default=0.0) if complete else None
        status = "complete" if complete else ("running" if running else "waiting")
        end = finished or (time.time() if running else None)
        candidate = {
            "status": status,
            "started_at": _trace_iso(started),
            "finished_at": _trace_iso(finished),
            "duration_s": round(max(0.0, end - started), 1) if end else None,
        }
        previous = timings.get(key)
        if previous:
            previous_start = _trace_timestamp(previous.get("started_at")) or started
            previous_finish = _trace_timestamp(previous.get("finished_at"))
            merged_start = min(previous_start, started)
            merged_finish = max(previous_finish or 0.0, finished or 0.0) or None
            merged_status = "running" if "running" in {previous.get("status"), status} else (
                "complete" if "complete" in {previous.get("status"), status} else "waiting"
            )
            merged_end = merged_finish or (time.time() if merged_status == "running" else None)
            candidate = {
                "status": merged_status,
                "started_at": _trace_iso(merged_start),
                "finished_at": _trace_iso(merged_finish),
                "duration_s": round(max(0.0, merged_end - merged_start), 1) if merged_end else None,
            }
        timings[key] = candidate
    known = [value for value in timings.values() if value.get("started_at")]
    if not known:
        return timings, None
    overall_start = min(_trace_timestamp(value["started_at"]) for value in known)
    active = any(value.get("status") == "running" for value in known)
    finishes = [_trace_timestamp(value.get("finished_at")) for value in known]
    complete_finishes = [value for value in finishes if value]
    overall_finish = None if active else (max(complete_finishes) if complete_finishes else None)
    overall_end = overall_finish or (time.time() if active else None)
    overall = {
        "started_at": _trace_iso(overall_start),
        "finished_at": _trace_iso(overall_finish),
        "duration_s": round(max(0.0, overall_end - overall_start), 1) if overall_end else None,
    }
    return timings, overall


def _run_response_language(run_dir):
    """Best-effort response language for filtering user-facing process prose."""
    candidates = [
        os.path.join(run_dir or "", "plan", "deck.md"),
        os.path.join(run_dir or "", "_trace", "orchestrator", "config.json"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8", errors="replace") as source:
                text = source.read(16384)
        except OSError:
            continue
        lowered = text.lower()
        if re.search(r"response[_ -]?language\s*[:：]\s*(zh|中文|chinese)", lowered):
            return "zh"
        if re.search(r"response[_ -]?language\s*[:：]\s*(en|英文|english)", lowered):
            return "en"
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        if cjk or latin:
            return "zh" if cjk * 4 >= latin else "en"
    return None


def _visible_text_for_language(text, language):
    """Hide model-default-language prose from the localized process UI.

    Raw traces stay untouched.  Tool events and deterministic role/status UI still
    describe progress when a weaker model ignores the requested response language.
    """
    value = str(text or "").strip()
    if not value or language not in {"zh", "en"}:
        return value
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if language == "zh" and not cjk and latin >= 8:
        return ""
    if language == "en" and cjk and cjk * 4 > max(latin, 1):
        return ""
    return value


def _append_subagent_summaries(
    agents, run_dir, next_sequence, aliases=None, response_language=None
):
    """Add each worker's durable final summary to its live activity stream.

    Runner logs intentionally keep only the first physical line of a multi-line
    final response.  The complete, user-facing summary is already persisted in
    ``summary.md``; merge that final result back into the stream without
    exposing raw model scratch messages.
    """
    trace_root = os.path.join(run_dir or "", "_trace", "subagents")
    if not os.path.isdir(trace_root):
        return
    for offset, agent_dir in enumerate(sorted(glob.glob(os.path.join(trace_root, "*")))):
        summary_path = os.path.join(agent_dir, "summary.md")
        try:
            with open(summary_path, encoding="utf-8", errors="replace") as source:
                summary = _visible_text_for_language(
                    source.read(2400), response_language
                )
        except OSError:
            continue
        if len(summary) < 12:
            continue
        key = _livefeed_agent_key(os.path.basename(agent_dir), aliases)
        # A continuation reuses the same workspace, so ``_trace/subagents`` may
        # still contain workers from earlier conversation turns.  Only enrich
        # agents that are present in this job log; otherwise an old Image Agent
        # is incorrectly moved into the newest assistant reply.
        if key not in agents:
            continue
        events = agents[key]
        text_events = [event for event in events if event.get("k") == "text"]
        last_text = text_events[-1] if text_events else None
        visible = str(last_text.get("s") or "").strip() if last_text else ""
        # The runner commonly logged only the first line. Replace that prefix
        # in-place so the final result keeps its real sequence position.
        if last_text and visible and summary.startswith(visible):
            last_text["s"] = summary
        elif not any(str(event.get("s") or "").strip() == summary for event in text_events):
            events.append({"k": "text", "s": summary, "seq": next_sequence + offset})


def livefeed(log_path, max_bytes=8 * 1024 * 1024, per_agent=240, run_dir=None):
    """Tail the runner log -> agents plus grouped page ownership.

    ``page_agents`` maps a page number to its ``slide_group_*`` owner when the
    grouped Skill is active.  Ordinary per-page Skills leave the map empty and
    continue to use ``slide_NN`` keys.

    Event: {"k":"tool","tool":name,"hint":path-or-snippet} | {"k":"text","s":...}.
    Retry agents merge into their base key; specialist roles stay separate so the
    Studio can reconstruct the complete orchestration conversation."""
    agents = {}
    page_agents = _plan_page_agents(run_dir)
    aliases = _trace_agent_aliases(run_dir)
    agent_timings, overall_timing = _agent_timing_payload(run_dir, aliases)
    response_language = _run_response_language(run_dir)
    if not log_path or not os.path.exists(log_path):
        return {
            "agents": agents,
            "page_agents": page_agents,
            "agent_timings": agent_timings,
            "overall_timing": overall_timing,
        }
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return {
            "agents": agents,
            "page_agents": page_agents,
            "agent_timings": agent_timings,
            "overall_timing": overall_timing,
        }
    for sequence, line in enumerate(chunk.split("\n")):
        m = _FEED_RE.match(line)
        if not m:
            continue
        raw_agent, tool, args, text = m.groups()
        key = _livefeed_agent_key(raw_agent, aliases)
        if key.startswith("slide_group_"):
            for page in _group_event_pages(args or ""):
                page_agents.setdefault(str(page), key)
        lst = agents.setdefault(key, [])
        if tool:
            hm = _HINT_RE.search(args or "")
            hint = hm.group(1) if hm else (args or "")[:48]
            lst.append({"k": "tool", "tool": tool, "hint": hint, "seq": sequence})
        else:
            t = _visible_text_for_language(text, response_language)
            # Old live logs and revision logs may contain the same progress
            # under both ``orch`` and ``orchestrator``.  They now share ``key``;
            # collapse an exact adjacent duplicate before it reaches the UI.
            if t and not (
                lst and lst[-1].get("k") == "text"
                and str(lst[-1].get("s") or "").strip() == t[:800].strip()
            ):
                lst.append({"k": "text", "s": t[:800], "seq": sequence})
    # stdout keeps the UI responsive before the first model response lands;
    # once the atomic trace exists, prefer its complete untruncated turns.
    for key, events in _live_trace_events(
        run_dir, aliases, response_language, set(agents)
    ).items():
        agents[key] = events
    _append_subagent_summaries(
        agents, run_dir, len(chunk.split("\n")) + 1, aliases, response_language
    )
    for k in agents:
        agents[k] = agents[k][-per_agent:]
    return {
        "agents": agents,
        "page_agents": page_agents,
        "agent_timings": agent_timings,
        "overall_timing": overall_timing,
    }


def specialist_artifacts(run_dir):
    """Small, user-facing artifact index for the 00 orchestration view.

    The browser receives only relative paths and mtimes.  Image bytes are served
    through the authenticated thumbnail endpoint, so polling the live feed never
    transfers the multi-megabyte originals produced by the Image Agent.
    """
    result = {
        "research": {"brief_ready": False, "queries": [], "sources": []},
        "material": {"count": 0, "files": []},
        "image": {"catalog_ready": False, "count": 0, "contact_sheet": None, "images": []},
        # Per-agent indexes prevent the orchestration view from attaching the
        # deck-wide gallery to every Image Agent card.  Older frontends can
        # continue to consume the role-level ``image`` entry above.
        "agents": {},
    }
    if not run_dir:
        return result
    # Different paired Skills use either one knowledge brief or several
    # research_NN briefs.  The UI reports the role artifact without imposing one
    # Skill's filename contract on the others.
    research_dir = os.path.join(run_dir, "research")
    result["research"]["brief_ready"] = any(
        os.path.isfile(path) and os.path.getsize(path) > 0
        for path in glob.glob(os.path.join(research_dir, "*.md"))
    )
    aliases = _trace_agent_aliases(run_dir)
    trace_root = os.path.join(run_dir, "_trace", "subagents")
    for agent_dir in sorted(glob.glob(os.path.join(trace_root, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        raw_agent = os.path.basename(agent_dir)
        agent = _livefeed_agent_key(raw_agent, aliases)
        role = agent.split("_", 1)[0]
        config = {}
        try:
            with open(os.path.join(agent_dir, "config.json"), encoding="utf-8", errors="replace") as source:
                config = json.load(source)
        except (OSError, ValueError, TypeError):
            pass
        if role == "research":
            queries = []
            try:
                with open(os.path.join(agent_dir, "tool_log.json"), encoding="utf-8", errors="replace") as source:
                    tool_log = json.load(source)
                for event in tool_log if isinstance(tool_log, list) else []:
                    if event.get("name") != "web_search":
                        continue
                    query = str((event.get("args") or {}).get("query") or "").strip()
                    if query and query not in queries:
                        queries.append(query)
            except (OSError, ValueError, TypeError):
                pass
            payload = ""
            for filename in ("messages.json", "summary.md"):
                path = os.path.join(agent_dir, filename)
                try:
                    if os.path.getsize(path) <= 8 * 1024 * 1024:
                        with open(path, encoding="utf-8", errors="replace") as source:
                            if filename.endswith(".json"):
                                payload += "\n" + "\n".join(_json_text_values(json.load(source)))
                            else:
                                payload += "\n" + source.read()
                except OSError:
                    continue
                except (ValueError, TypeError):
                    continue
            sources = []
            seen_urls = set()
            titled = re.compile(r"-\s+([^\n]{2,180})\n\s*(https?://[^\s\"'<>]+)", re.I)
            for match in titled.finditer(payload):
                title = re.sub(r"\\[nrt]", " ", match.group(1)).strip(" -")
                url = match.group(2).rstrip(".,;，；")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append({"title": title, "url": url})
            for url in re.findall(r"https?://[^\\\s\"'<>]+", payload, re.I):
                url = url.rstrip(".,;，；")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                sources.append({"title": re.sub(r"^https?://", "", url).split("/", 1)[0], "url": url})
            artifact = {
                "brief_ready": result["research"]["brief_ready"],
                "queries": queries[:24],
                "sources": sources[:24],
            }
            result["agents"][agent] = artifact
            for query in artifact["queries"]:
                if query not in result["research"]["queries"]:
                    result["research"]["queries"].append(query)
            known = {item["url"] for item in result["research"]["sources"]}
            result["research"]["sources"].extend(
                item for item in artifact["sources"] if item["url"] not in known
            )
        elif role == "material":
            task = str(config.get("task") or config.get("goal") or "")
            names = []
            for match in re.findall(
                r"[^\s\"'`<>]+?\.(?:pdf|docx?|pptx?|xlsx?|csv|txt|md|png|jpe?g|webp)",
                task,
                re.I,
            ):
                name = os.path.basename(match.rstrip(".,;，；)"))
                if name and name not in names:
                    names.append(name)
            files = [{"name": name, "status": "processing"} for name in names]
            result["agents"][agent] = {"count": len(files), "files": files}

    material_files = []
    for catalog_path in glob.glob(os.path.join(run_dir, "materials", "**", "catalog.json"), recursive=True):
        try:
            with open(catalog_path, encoding="utf-8", errors="replace") as source:
                catalog = json.load(source)
        except (OSError, ValueError, TypeError):
            continue
        entries = catalog.get("entries", []) if isinstance(catalog, dict) else catalog
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("from_scanned_pdf"):
                continue
            coverage = entry.get("coverage") or ""
            item = {
                "name": str(entry.get("name") or os.path.basename(str(entry.get("raw") or "material"))),
                "kind": str(entry.get("kind") or "file"),
                "status": str(entry.get("status") or "unknown"),
                "coverage": str(coverage.get("status") or "") if isinstance(coverage, dict) else str(coverage),
            }
            if item not in material_files:
                material_files.append(item)
    result["material"] = {"count": len(material_files), "files": material_files}
    material_agents = [key for key in result["agents"] if key.startswith("material")]
    if material_files:
        if len(material_agents) == 1:
            result["agents"][material_agents[0]] = result["material"]
        elif not material_agents:
            result["agents"]["material"] = result["material"]
    asset_dir = os.path.join(run_dir, "assets")
    result["image"]["catalog_ready"] = any(
        os.path.isfile(os.path.join(asset_dir, name))
        for name in ("catalog.json", "catalog.md")
    )
    if not os.path.isdir(asset_dir):
        return result

    provenance = {}
    has_asset_status_contract = False
    try:
        with open(os.path.join(asset_dir, "catalog.json"), encoding="utf-8", errors="replace") as source:
            asset_catalog = json.load(source)
        entries = asset_catalog.get("assets", []) if isinstance(asset_catalog, dict) else []
        provenance = {
            str(item.get("path")): item for item in entries
            if isinstance(item, dict) and item.get("path")
        }
        has_asset_status_contract = any(
            isinstance(item, dict) and (item.get("asset_id") or "status" in item)
            for item in entries
        )
    except (OSError, ValueError, TypeError):
        pass

    raster_exts = {".png", ".jpg", ".jpeg", ".webp"}
    raw_images = []
    contact_sheets = []
    for path in glob.glob(os.path.join(asset_dir, "*")):
        if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in raster_exts:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        item = {
            "path": "assets/" + os.path.basename(path),
            "name": os.path.basename(path),
            "mtime": int(stat.st_mtime),
        }
        meta = provenance.get(item["path"], {})
        if isinstance(meta, dict):
            for key in (
                "origin", "source_url", "source_path", "generator_model",
                "parent_asset", "source_origin", "aspect_ratio", "asset_id",
                "group_id", "status",
            ):
                if meta.get(key):
                    item[key] = meta[key]
        basename = os.path.basename(path).lower()
        if basename == "contact-sheet.png" or basename.startswith("contact-sheet-"):
            contact_sheets.append(item)
        else:
            item["_full_path"] = path
            item["_stat"] = stat
            raw_images.append(item)

    # Collapse byte-identical aliases while retaining every path in the
    # lookup below. Prefer a human-readable semantic filename over the
    # image_generate hash, and merge provenance from the catalogued original.
    by_size = {}
    for item in raw_images:
        by_size.setdefault(item["_stat"].st_size, []).append(item)
    duplicate_groups = {}
    for size, same_size in by_size.items():
        if len(same_size) == 1:
            item = same_size[0]
            duplicate_groups[(size, item["path"])] = [item]
            continue
        for item in same_size:
            try:
                digest = _asset_digest(item["_full_path"], item["_stat"])
            except OSError:
                digest = item["path"]
            duplicate_groups.setdefault((size, digest), []).append(item)

    image_aliases = {}
    images = []
    generated_hash_name = re.compile(r"^img_[0-9a-f]{8,}\.(?:png|jpe?g|webp)$", re.I)
    provenance_keys = (
        "origin", "source_url", "source_path", "generator_model",
        "parent_asset", "source_origin", "aspect_ratio", "asset_id",
        "group_id", "status",
    )
    for group in duplicate_groups.values():
        canonical = max(group, key=lambda item: (
            not bool(generated_hash_name.match(item["name"])),
            sum(bool(item.get(key)) for key in provenance_keys),
            item["mtime"],
        ))
        for item in group:
            for key in provenance_keys:
                if not canonical.get(key) and item.get(key):
                    canonical[key] = item[key]
        canonical.pop("_full_path", None)
        canonical.pop("_stat", None)
        images.append(canonical)
        for item in group:
            image_aliases[item["path"]] = canonical
    images.sort(key=lambda item: (item["mtime"], item["name"]))
    if contact_sheets:
        contact_sheets.sort(key=lambda item: (item["mtime"], item["name"]))
        result["image"]["contact_sheet"] = contact_sheets[-1]
    ready_ids = {
        str(item.get("asset_id")) for item in images
        if item.get("status") == "ready" and item.get("asset_id")
    }
    result["image"]["count"] = len(ready_ids) if has_asset_status_contract else len(images)
    # Keep the complete asset set.  The browser requests lazy thumbnails, so
    # dropping older items here only makes the material timeline misleading:
    # once a contact sheet exists the user still needs to see every source
    # image that was collected/generated before the whole-set review.
    result["image"]["images"] = images

    image_items = {item["path"]: item for item in images}
    image_items.update(image_aliases)
    for sheet in contact_sheets:
        image_items[sheet["path"]] = sheet

    image_path_pattern = re.compile(
        r"assets/[^\s\"'`<>()?#[\]{}]+?\.(?:png|jpe?g|webp)", re.I
    )

    def image_paths(value):
        """Extract workspace image paths from one structured trace value."""
        found = []
        if isinstance(value, str):
            for match in image_path_pattern.finditer(value):
                rel = match.group(0).replace("\\", "/")
                if rel not in found:
                    found.append(rel)
        elif isinstance(value, list):
            for entry in value:
                for rel in image_paths(entry):
                    if rel not in found:
                        found.append(rel)
        elif isinstance(value, dict):
            for entry in value.values():
                for rel in image_paths(entry):
                    if rel not in found:
                        found.append(rel)
        return found

    def generated_paths_from_messages(trace_path):
        """Return only image-tool outputs, excluding files merely read by an agent.

        Image agents often inspect the deck-wide ``assets/catalog.json``.  A raw
        regex over the complete conversation consequently attributes every
        catalog entry to the last agent that read it.  Tool-use/result linkage is
        the authoritative ownership signal: keep paths returned by that agent's
        image generation/search tools and ignore unrelated read-file results.
        """
        try:
            if os.path.getsize(trace_path) > 8 * 1024 * 1024:
                return [], False
            with open(trace_path, "r", encoding="utf-8", errors="replace") as source:
                messages = json.load(source)
        except (OSError, ValueError, TypeError):
            return [], False
        if not isinstance(messages, list):
            return [], False

        image_tool_ids = set()
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "").lower()
                if name in {
                    "image_generate",
                    "image_search",
                    "image_download",
                    # The current long-horizon image role downloads selected
                    # search results with fetch_image.  Treat its linked result
                    # as first-class ownership evidence; otherwise this agent
                    # falls back to scanning messages and inherits every path
                    # from a deck-wide catalog it happened to read.
                    "fetch_image",
                }:
                    tool_id = str(block.get("id") or "")
                    if tool_id:
                        image_tool_ids.add(tool_id)

        paths = []
        if image_tool_ids:
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    if str(block.get("tool_use_id") or "") not in image_tool_ids:
                        continue
                    for rel in image_paths(block.get("content")):
                        if rel not in paths:
                            paths.append(rel)

            # A contact sheet can be assembled after generation without another
            # image tool call.  Keep that one explicit review artifact when the
            # agent's final response names it.
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                for rel in image_paths(message.get("content")):
                    name = os.path.basename(rel).lower()
                    if (name == "contact-sheet.png" or name.startswith("contact-sheet-")) and rel not in paths:
                        paths.append(rel)
        return paths, bool(image_tool_ids)

    per_agent_paths = {}
    for agent_dir in sorted(glob.glob(os.path.join(trace_root, "*"))):
        if not os.path.isdir(agent_dir):
            continue
        raw_agent = os.path.basename(agent_dir)
        agent = _livefeed_agent_key(raw_agent, aliases)
        if not agent.startswith("image"):
            continue
        paths = per_agent_paths.setdefault(agent, [])
        message_path = os.path.join(agent_dir, "messages.json")
        generated_paths, has_image_tools = generated_paths_from_messages(message_path)
        if has_image_tools:
            for rel in generated_paths:
                if rel in image_items and rel not in paths:
                    paths.append(rel)
            continue

        # Compatibility path for older traces that did not preserve tool-use
        # IDs.  Those runs can only be reconstructed from the paths mentioned in
        # their compact logs/summaries.
        for filename in ("tool_log.json", "messages.json"):
            trace_path = os.path.join(agent_dir, filename)
            try:
                if os.path.getsize(trace_path) > 2 * 1024 * 1024:
                    continue
                with open(trace_path, "r", encoding="utf-8", errors="ignore") as stream:
                    payload = stream.read()
            except OSError:
                continue
            for match in image_path_pattern.finditer(payload):
                rel = match.group(0).replace("\\", "/")
                if rel in image_items and rel not in paths:
                    paths.append(rel)

    for agent, paths in per_agent_paths.items():
        scoped_images = []
        scoped_seen = set()
        for path in paths:
            item = image_items.get(path)
            if not item or item["path"] in scoped_seen:
                continue
            scoped_seen.add(item["path"])
            scoped_images.append(item)
        contact_sheet = next(
            (
                item for item in reversed(scoped_images)
                if item["name"].lower() == "contact-sheet.png"
                or item["name"].lower().startswith("contact-sheet-")
            ),
            None,
        )
        scoped_images = [
            item for item in scoped_images
            if item["name"].lower() != "contact-sheet.png"
            and not item["name"].lower().startswith("contact-sheet-")
        ]
        scoped_ready_ids = {
            str(item.get("asset_id")) for item in scoped_images
            if item.get("status") == "ready" and item.get("asset_id")
        }
        agent_trace_dir = next(
            (
                path for path in glob.glob(os.path.join(trace_root, "*"))
                if os.path.isdir(path)
                and _livefeed_agent_key(os.path.basename(path), aliases) == agent
            ),
            "",
        )
        result["agents"][agent] = {
            "catalog_ready": result["image"]["catalog_ready"],
            # summary.md is written only after that concrete Image child has
            # actually stopped.  A deck-wide catalog may already exist while
            # another Image child is still running, so it cannot mark every
            # card complete.
            "completed": bool(
                agent_trace_dir
                and os.path.isfile(os.path.join(agent_trace_dir, "summary.md"))
            ),
            "count": len(scoped_ready_ids) if has_asset_status_contract else len(scoped_images),
            "contact_sheet": contact_sheet,
            "images": scoped_images,
        }
    return result


def scoped_specialist_artifacts(run_dir, agent_keys):
    """Return specialist artifacts belonging to one archived execution turn.

    In-place revisions intentionally keep generated assets and traces in one
    workspace.  The complete artifact index therefore spans several turns;
    scope it to the agents found in the corresponding immutable job log before
    exposing it in the conversation timeline.
    """
    artifacts = specialist_artifacts(run_dir)
    wanted = {str(key) for key in (agent_keys or [])}
    agents = artifacts.get("agents") if isinstance(artifacts.get("agents"), dict) else {}
    artifacts["agents"] = {
        key: value for key, value in agents.items() if key in wanted
    }
    roles = {key.split("_", 1)[0].split("-", 1)[0] for key in wanted}
    if "research" not in roles:
        artifacts["research"] = {"brief_ready": False, "queries": [], "sources": []}
    if "material" not in roles:
        artifacts["material"] = {"count": 0, "files": []}
    if "image" not in roles:
        artifacts["image"] = {
            "catalog_ready": False, "count": 0, "contact_sheet": None, "images": []
        }
    return artifacts


# ---- 页级视觉迭代历史：真实 Vision 快照 + 判断 + 后续修改 -----------------
_TRACE_VISION_RE = re.compile(r"🔧\s+vision_analyze\((.*)\)\s*$", re.S)
_TRACE_RENDER_RE = re.compile(r"deck\.py\s+render\s+\.\s+--page\s+0?(\d+)\b")
_TRACE_PAGE_IMAGE_RE = re.compile(r"(?:slide_|page_)0?(\d+)\.png\b", re.I)
_TRACE_CHAT_RE = re.compile(r"(?:\[\d+\]\s+)?💬\s+(.*)", re.S)
_TRACE_TOOL_RE = re.compile(r"🔧\s+(patch|edit|write_file)\((.*)\)\s*$", re.S)
_SPEECH_HEADING_RE = re.compile(
    r"^#{1,3}\s+(?:(?:slide|page)\s*0*(?P<western>\d+)|第\s*0*(?P<cjk>\d+)\s*页)"
    r"(?:\s*[—–:：|｜·-]\s*(?P<title>[^\n]+))?\s*$",
    re.I | re.M,
)
_SPEECH_EVIDENCE_RE = re.compile(
    r"^#{2,4}\s+(?:evidence\s+and\s+sources|证据(?:与来源)?|来源(?:与证据)?|"
    r"参考资料(?:[（(]不朗读[）)])?)\s*$",
    re.I | re.M,
)
_SPEECH_CONTENT_RE = re.compile(
    r"^#{2,4}\s+(?:讲述内容|演讲稿|speaker\s+notes?)\s*$", re.I | re.M)


def _trace_arg(payload, key):
    """Best-effort extraction from a possibly truncated JSON tool call."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', payload or "", re.S)
    if not match:
        # Trace messages intentionally cap long tool arguments.  Preserve the
        # visible prefix so revision summaries can still identify selectors and
        # CSS properties even when the closing JSON quote was truncated.
        prefix = re.search(rf'"{re.escape(key)}"\s*:\s*"(.*)', payload or "", re.S)
        if not prefix:
            return ""
        return prefix.group(1).replace(r"\n", "\n").replace(r'\"', '"').rstrip(")")
    try:
        return json.loads('"' + match.group(1) + '"')
    except (TypeError, ValueError, json.JSONDecodeError):
        return match.group(1).replace(r"\n", " ").replace(r'\"', '"')


def _clean_history_text(value, limit=700):
    text = str(value or "").strip()
    text = re.sub(r"\[context compacted\].*", "", text, flags=re.I | re.S)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _review_page_excerpt(text, page):
    """Pull P01's paragraph from an all-deck review summary when available."""
    pattern = re.compile(
        rf"(?:^|\n)\s*-?\s*\*\*P0?{page}\b.*?\*\*\s*[：:]?\s*(.*?)"
        rf"(?=\n\s*-?\s*\*\*P\d+\b|\Z)", re.S | re.I)
    match = pattern.search(text or "")
    if match:
        return _clean_history_text(match.group(1))
    # v3 review agents usually write ``slide_12`` rather than ``P12``. Keep
    # the matching paragraph only, instead of showing the whole deck review.
    slide_ref = re.compile(rf"\bslide[_\s-]*0?{page}\b", re.I)
    for paragraph in re.split(r"\n\s*\n", text or ""):
        if slide_ref.search(paragraph):
            return _clean_history_text(paragraph)
    return ""


def _history_image_rel(run_dir, label, view_no):
    roots = []
    canonical = _canonical_agent_key(label)
    if canonical == "orch":
        # The on-disk directory remains ``orchestrator`` for compatibility;
        # only the public identity is canonicalized to ``orch``.
        roots.extend([
            os.path.join(run_dir, "_trace", "orchestrator"),
            os.path.join(run_dir, "_trace", "orch"),
        ])
    roots.extend([
        os.path.join(run_dir, "_trace", "subagents", label),
        os.path.join(run_dir, "_trace", label),
    ])
    for root in roots:
        for name in (f"view_{view_no:03d}.png", f"view_{view_no:02d}.png", f"view_{view_no}.png"):
            path = os.path.join(root, "images", name)
            if os.path.isfile(path):
                return os.path.relpath(path, run_dir).replace(os.sep, "/")
    return ""


def _change_note(tool_name, payload):
    """Turn an observable file edit into a short, user-facing revision note.

    The harness does not persist a separate Vision prose response: the model sees
    the PNG and immediately edits the slide.  We therefore describe the actual
    edits made after that inspection instead of exposing model scratchpad text.
    """
    if tool_name == "write_file":
        return "重写本页 HTML 结构与样式"
    old = _trace_arg(payload, "old_string")
    new = _trace_arg(payload, "new_string")
    joined = f"{old}\n{new}"
    selectors = []
    for match in re.finditer(r"(?:^|\n)\s*([^\n{}]{1,90})\s*\{", joined):
        selector = re.sub(r"\s+", " ", match.group(1)).strip()
        if (selector.startswith((".", "#")) or " #" in selector or " ." in selector) \
                and selector not in selectors:
            selectors.append(selector)
    props = []
    old_props = dict(re.findall(r"(?:^|[;{]\s*|\n\s*)([-\w]+)\s*:\s*([^;\n}]+)", old))
    new_props = dict(re.findall(r"(?:^|[;{]\s*|\n\s*)([-\w]+)\s*:\s*([^;\n}]+)", new))
    for prop in dict.fromkeys([*old_props, *new_props]):
        if old_props.get(prop) != new_props.get(prop):
            props.append(prop)
    target = selectors[0] if selectors else "页面内容"
    if props:
        return f"调整 {target} 的{'、'.join(props[:4])}"
    if re.search(r"<[^>]+>", joined):
        classes = re.findall(r'class=\\?"([^"\\]+)', joined)
        if classes:
            return f"调整 {classes[-1].split()[0]} 区域的结构与内容"
        return "调整页面结构与内容"
    return "微调本页视觉细节"


def page_speech(run_dir, page):
    """Return the speech.md section that belongs to one slide.

    Current Skills write ``speech.md`` at the Deck root.  ``plan/speech.md`` is
    retained as a compatibility fallback for historical workspaces.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"exists": False, "page": 0}
    if page <= 0:
        return {"exists": False, "page": page}

    speech_path = next((
        candidate for candidate in (
            os.path.join(run_dir, "speech.md"),
            os.path.join(run_dir, "plan", "speech.md"),
        ) if os.path.isfile(candidate)
    ), "")
    if not speech_path:
        return {"exists": False, "page": page}
    try:
        with open(speech_path, encoding="utf-8", errors="replace") as speech_file:
            text = speech_file.read()
    except OSError:
        return {"exists": False, "page": page}

    headings = list(_SPEECH_HEADING_RE.finditer(text))
    for index, heading in enumerate(headings):
        number = int(heading.group("western") or heading.group("cjk") or 0)
        if number != page:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end():end].strip()
        evidence_match = _SPEECH_EVIDENCE_RE.search(body)
        notes = body[:evidence_match.start()].strip() if evidence_match else body
        evidence = body[evidence_match.end():].strip() if evidence_match else ""
        # v3 emits a page heading followed by ``## 讲述内容``.  That structural
        # heading is useful in the artifact but should not be repeated in the UI.
        notes = _SPEECH_CONTENT_RE.sub("", notes, count=1).strip()
        return {
            "exists": True,
            "page": page,
            "title": (heading.group("title") or "").strip(),
            "notes": notes,
            "evidence": evidence,
            "source": os.path.relpath(speech_path, run_dir).replace(os.sep, "/"),
        }
    return {"exists": False, "page": page, "source": os.path.relpath(speech_path, run_dir).replace(os.sep, "/")}


def _read_trace_json(path, default):
    try:
        with open(path, encoding="utf-8", errors="replace") as source:
            value = json.load(source)
        return value
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default


def _assistant_visible_text(message):
    """Return only user-visible assistant text from a persisted message.

    The message trace may also contain tool calls and private reasoning blocks.
    Page-history cards should expose the assistant's stated visual verdict, not
    implementation payloads or hidden scratchpad content.
    """
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"text", "output_text"}:
            continue
        text = str(block.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _trace_result_shot(value):
    """Return the exact persisted screenshot referenced by a tool result."""
    if isinstance(value, dict):
        shot = value.get("shot")
        if isinstance(shot, str) and shot.strip():
            return shot.strip()
        for child in value.values():
            found = _trace_result_shot(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _trace_result_shot(child)
            if found:
                return found
    return ""


def _v3_message_image_rel(run_dir, agent_dir, shot):
    """Resolve a message ``shot`` safely to a Deck-relative trace path."""
    shot = str(shot or "").strip().replace("\\", "/")
    if not shot:
        return ""
    candidate = os.path.realpath(os.path.join(agent_dir, shot))
    agent_root = os.path.realpath(agent_dir)
    try:
        if os.path.commonpath([candidate, agent_root]) != agent_root:
            return ""
    except ValueError:
        return ""
    if not os.path.isfile(candidate):
        return ""
    return os.path.relpath(candidate, run_dir).replace(os.sep, "/")


def _v3_vision_judgments(agent_dir):
    """Recover the visible verdict following each v3 ``vision_analyze`` call.

    ``tool_log.json`` records the image and question, while the actual verdict
    is persisted in ``messages.json`` after the corresponding tool result. Keep
    the result ordered exactly like the tool log so callers can join the two
    immutable traces without relying on mutable render filenames.
    """
    messages = _read_trace_json(os.path.join(agent_dir, "messages.json"), [])
    if not isinstance(messages, list):
        return []

    calls = []
    by_id = {}
    waiting = []
    is_review = bool(re.search(r"(?:^|[-_])review(?:[-_]|$)", os.path.basename(agent_dir)))
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        blocks = content if isinstance(content, list) else []

        if role == "assistant":
            visible = _assistant_visible_text(message)
            if visible and waiting:
                referenced_pages = {
                    int(value) for value in re.findall(
                        r"(?:\bslide[_\s-]*|\bP0?|第\s*)(\d{1,3})(?:\s*页)?\b", visible, re.I)
                }
                for call in waiting:
                    page = call.get("page") or 0
                    excerpt = _review_page_excerpt(visible, page) if is_review and page else ""
                    if is_review and len(waiting) > 1 and referenced_pages and page not in referenced_pages:
                        call["judgment"] = "本轮批量视觉检查已完成，未单独指出本页问题。"
                    else:
                        call["judgment"] = _clean_history_text(excerpt or visible)
                waiting = []

            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") != "vision_analyze":
                    continue
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                image_path = str(args.get("image_url") or args.get("path") or "")
                page_match = _TRACE_PAGE_IMAGE_RE.search(image_path)
                call = {
                    "id": str(block.get("id") or ""),
                    "page": int(page_match.group(1)) if page_match else 0,
                    "judgment": "",
                    "shot": "",
                }
                calls.append(call)
                if call["id"]:
                    by_id[call["id"]] = call
            continue

        if role != "user":
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call = by_id.get(str(block.get("tool_use_id") or ""))
            if call is not None:
                call["shot"] = _trace_result_shot(block)
                if call not in waiting:
                    waiting.append(call)
    return calls


def _v3_page_history(run_dir, page, max_events=24):
    """Read page checks from the v3 per-agent trace layout.

    v3 intentionally persists each agent independently (``tool_log.json`` plus
    ``images/view_01.png``) and does not create the older aggregate
    ``_trace/events.jsonl``.  Reconstruct the same UI contract from those
    immutable records without rewriting a completed workspace.
    """
    agents_root = os.path.join(run_dir, "_trace", "subagents")
    if not os.path.isdir(agents_root):
        return {"page": page, "items": [], "total": 0}

    agent_dirs = [path for path in glob.glob(os.path.join(agents_root, "*")) if os.path.isdir(path)]

    def agent_epoch(path):
        candidates = []
        for name in ("config.json", "tool_log.json", "messages.json", "summary.md"):
            try:
                candidates.append(os.path.getmtime(os.path.join(path, name)))
            except OSError:
                pass
        if candidates:
            return min(candidates)
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    revision_boundaries = []
    for request_path in glob.glob(os.path.join(
            run_dir, "_trace", "revisions", "revision_*", "request.json")):
        request = _read_trace_json(request_path, {})
        try:
            revision_no = int(request.get("revision_no") or 0)
            created_at = float(request.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if revision_no > 0 and created_at > 0:
            revision_boundaries.append((created_at, revision_no))
    revision_boundaries.sort()

    agent_dirs.sort(key=lambda path: (agent_epoch(path), os.path.basename(path)))
    items = []
    item_order = 0
    for agent_dir in agent_dirs:
        label = os.path.basename(agent_dir)
        base_epoch = agent_epoch(agent_dir)
        revision_no = 0
        for created_at, candidate in revision_boundaries:
            if base_epoch >= created_at:
                revision_no = candidate
            else:
                break
        calls = _read_trace_json(os.path.join(agent_dir, "tool_log.json"), [])
        if not isinstance(calls, list):
            continue
        try:
            with open(os.path.join(agent_dir, "summary.md"), encoding="utf-8", errors="replace") as summary_file:
                summary = summary_file.read()
        except OSError:
            summary = ""
        is_review = bool(re.search(r"(?:^|[-_])review(?:[-_]|$)", label))
        message_judgments = _v3_vision_judgments(agent_dir)
        agent_items = []
        vision_no = 0
        render_no = 0
        active_item = None
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            command = str(args.get("command") or "")
            if name in {"terminal", "run_command", "exec_command"} and re.search(
                    rf"(?:slides|renders)/slide_0?{page}\.(?:html|png)\b", command, re.I):
                render_no += 1
            if name == "vision_analyze":
                vision_no += 1
                image_path = str(args.get("image_url") or args.get("path") or "")
                page_match = _TRACE_PAGE_IMAGE_RE.search(image_path)
                target_page = int(page_match.group(1)) if page_match else 0
                active_item = None
                if target_page != page:
                    continue
                message_judgment = ""
                message_image_rel = ""
                has_message_record = vision_no <= len(message_judgments)
                if has_message_record:
                    message_record = message_judgments[vision_no - 1]
                    message_judgment = str(message_record.get("judgment") or "")
                    message_image_rel = _v3_message_image_rel(
                        run_dir, agent_dir, message_record.get("shot"))
                judgment = message_judgment or (
                    _review_page_excerpt(summary, page) if is_review else _clean_history_text(summary))
                item = {
                    "stage": "review" if is_review else "build",
                    "time": "",
                    "epoch": base_epoch + vision_no / 1000,
                    "revision_no": revision_no,
                    "agent": "整册 Review Agent" if is_review else f"页面 {page:02d} 子 Agent",
                    "render_no": render_no,
                    # Some failed Vision calls do not persist a screenshot, so
                    # the image ordinal can diverge from the tool-call ordinal.
                    # messages.json retains the exact call-id -> shot mapping.
                    "image_rel": (message_image_rel if has_message_record else
                                  _history_image_rel(run_dir, label, vision_no)),
                    "prompt": _clean_history_text(args.get("question") or args.get("prompt"), 500),
                    "judgment": judgment,
                    "changes": 0,
                    "change_notes": [],
                }
                item_order += 1
                item["_order"] = item_order
                items.append(item)
                agent_items.append(item)
                active_item = item
                continue
            if active_item is None or name not in {"patch", "edit", "write_file"}:
                continue
            path = str(args.get("path") or "")
            if not re.search(rf"(?:^|/)slide_0?{page}\.html\b", path, re.I):
                continue
            active_item["changes"] += 1
            payload = json.dumps(args, ensure_ascii=False)
            note = _change_note(name, payload)
            if note and note not in active_item["change_notes"] and len(active_item["change_notes"]) < 4:
                active_item["change_notes"].append(note)

        for item in agent_items:
            if item.get("judgment") or not item.get("changes"):
                continue
            notes = "；".join(item.get("change_notes") or [])
            item["judgment"] = f"检查发现页面仍需调整，已完成 {notes or '视觉细节修正'}。"

    items.sort(key=lambda item: (item.get("epoch") or 0, item.get("_order") or 0))
    for index, item in enumerate(items, 1):
        item["version"] = index
        item["image_url"] = item.pop("image_rel", "")
        item.pop("_order", None)
    return {"page": page, "items": items[-max_events:], "total": len(items)}


def page_history(run_dir, page, max_events=24):
    """Rebuild one slide's real visual iteration timeline from _trace/events.jsonl.

    Every vision_analyze input is already snapshotted by the harness as
    _trace/<agent>/images/view_NNN.png. Those immutable images are the exact
    render versions the agent judged, unlike renders/slide_N.png which is
    overwritten on every pass.
    """
    try:
        page = int(page)
    except (TypeError, ValueError):
        return {"page": 0, "items": []}
    if page <= 0:
        return {"page": page, "items": []}
    event_path = os.path.join(run_dir, "_trace", "events.jsonl")
    if not os.path.isfile(event_path):
        return _v3_page_history(run_dir, page, max_events=max_events)

    items = []
    view_counts = {}
    render_counts = {}
    active = {}
    try:
        lines = open(event_path, encoding="utf-8", errors="replace")
    except OSError:
        return {"page": page, "items": []}
    with lines:
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            label = str(event.get("label") or "")
            message = str(event.get("message") or "")
            if not label or not message:
                continue

            render_match = _TRACE_RENDER_RE.search(message)
            if render_match:
                rendered_page = int(render_match.group(1))
                key = (label, rendered_page)
                render_counts[key] = render_counts.get(key, 0) + 1

            vision = _TRACE_VISION_RE.search(message)
            if vision:
                view_counts[label] = view_counts.get(label, 0) + 1
                payload = vision.group(1)
                image_path = _trace_arg(payload, "image_url") or _trace_arg(payload, "path")
                page_match = _TRACE_PAGE_IMAGE_RE.search(image_path)
                target_page = int(page_match.group(1)) if page_match else 0
                # Contact sheets are intentionally not duplicated into every page.
                if target_page != page:
                    continue
                stage = "review" if re.search(r"(?:^|_)review(?:_|$)", label) else "build"
                item = {
                    "stage": stage,
                    "time": event.get("clock") or "",
                    "epoch": event.get("epoch"),
                    "agent": "整册 Review Agent" if stage == "review" else f"页面 {page:02d} 子 Agent",
                    "render_no": render_counts.get((label, page), 0),
                    "image_rel": _history_image_rel(run_dir, label, view_counts[label]),
                    "prompt": _clean_history_text(
                        _trace_arg(payload, "question") or _trace_arg(payload, "prompt"), 500),
                    "judgment": "",
                    "changes": 0,
                    "change_notes": [],
                }
                items.append(item)
                active[label] = item
                continue

            tool = _TRACE_TOOL_RE.search(message)
            if tool and label in active:
                payload = tool.group(2)
                path = _trace_arg(payload, "path")
                label_page = re.search(r"slide_0?(\d+)", label)
                owns_page = bool(label_page and int(label_page.group(1)) == page)
                if owns_page or re.search(rf"slides/slide_0?{page}\.html\b", path):
                    active[label]["changes"] += 1
                    note = _change_note(tool.group(1), payload)
                    if note and note not in active[label]["change_notes"] \
                            and len(active[label]["change_notes"]) < 4:
                        active[label]["change_notes"].append(note)
                continue

            chat = _TRACE_CHAT_RE.search(message)
            if not chat:
                continue
            text = chat.group(1).strip()
            if re.search(r"(?:^|[-_])review(?:[-_]|$)", label):
                excerpt = _review_page_excerpt(text, page)
                if excerpt:
                    for item in reversed(items):
                        if item["stage"] == "review" and not item["judgment"]:
                            item["judgment"] = excerpt
            elif label in active and not active[label]["judgment"]:
                clean = _clean_history_text(text)
                if len(clean) >= 12:
                    active[label]["judgment"] = clean

    # Stable page-local version number across build and review passes.
    for index, item in enumerate(items, 1):
        item["version"] = index
        item["image_url"] = item.pop("image_rel", "")
    return {"page": page, "items": items[-max_events:], "total": len(items)}
