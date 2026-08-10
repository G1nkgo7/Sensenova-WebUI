#!/usr/bin/env python3
"""单 sample 的多 Agent rollout —— 编排器(只规划+委派)+ 专门 subagent(调研/配图/slide/复审)。

一个 deck(一个 sample)= 一个**编排器**,专注规划:吃透 brief、全局规划(plan/)、设计系统
(base.css)、每页规划。其余环节都通过 `delegate_task` 委派给专门 subagent——前置的**调研**
(web → fact pack)与**配图**(image_gen+vision → 配图路径),并行的 **slide**(每页一个,写/渲/
自纠),收尾的**复审**(vision → 问题清单)。子 agent 类型不预定义,由 goal + toolsets 现拼。
每个 subagent 在自己的上下文里干活,只把一段文本总结(含截图路径)返回给编排器;截图本身留在
subagent 上下文里(上下文隔离)。编排器自己**不渲染、不看图**,据 subagent 返回的报告做决策。

进程/线程模型:sample 之间 = 进程(distill.py 调度);一个 sample 内的 slide subagent =
线程(每次 delegate_task 起一个**本地** ThreadPoolExecutor,用完即关,不留模块级全局池 →
不会跨 sample 串台)。render 在**子进程**里跑(skill 自带脚本 skills/ppt-skill/scripts/render.py)。

轨迹记录:编排器和每个 subagent 各写各的原始轨迹到 `_trace/` 下(orchestrator/、
subagents/slide_NN/),每个 (上下文 -> 输出) 对都忠实保留。图像在**被查看的当下**快照到
该轨迹的 images/ 里(因为 render 会覆盖 renders/slide_NN.png,只存路径会丢真),保证回放忠实。

拒绝采样:run_sample 只在轨迹**结构上干净**时返回 "completed",否则 "rejected"(脏数据丢)。
recorder(把原始轨迹转成 SFT 格式)稍后再写;本文件先把原始轨迹落全,自带验收逻辑。
"""
import base64
import concurrent.futures as cf
import glob
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

import anthropic

import tools
from attachments_runtime import build_initial_user_content

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.abspath(os.environ.get("SKILLS_DIR", os.path.join(ROOT, "skills")))
PPT_SKILL_DIR = os.path.abspath(os.environ.get("PPT_SKILL_DIR", os.path.join(SKILLS_DIR, "ppt-skill")))

# 子 agent 并发上限(slide-writer / image-curator 共用同一池)。对齐 hermes 语义改名,
# 保留旧 env 名 SLIDE_CONCURRENCY 兼容(restart_distill.sh 仍在用)。
MAX_CONCURRENT_CHILDREN = int(os.environ.get("MAX_CONCURRENT_CHILDREN",
                              os.environ.get("SLIDE_CONCURRENCY", "4")))
SLIDE_CONCURRENCY = MAX_CONCURRENT_CHILDREN                      # 兼容别名
WORKER_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "600"))    # 单个 subagent 硬超时(秒)
# 配图 subagent 要串行跑多张 gpt-image-2(每张约 60-90s)+ vision 自核对 + 跑色重生成,
# 经常 >600s;沿用 600s 会把已干完活的 image subagent 误判 timeout → 整条 image-rich deck 被误拒。
IMAGE_WORKER_TIMEOUT = int(os.environ.get("IMAGE_WORKER_TIMEOUT", "1800"))  # 含 image_gen 的 subagent 硬超时(秒)
MAX_HEALS = int(os.environ.get("MAX_HEALS", "2"))               # 空/截断回合的有限自愈次数
MAX_SPAWN_DEPTH = int(os.environ.get("MAX_SPAWN_DEPTH", "1"))   # 委派深度上限(1 = 只有顶层编排器能委派)
CHILD_MAX_ATTEMPTS = max(1, int(os.environ.get("CHILD_MAX_ATTEMPTS", "2")))

# 通用存活守护。只判断“同一动作/结果/错误是否重复且工作区无变化”，
# 不包含任何 PPT 内容规则，因此不会限制正常的长任务。
STALL_IDENTICAL_TURNS = max(2, int(os.environ.get("STALL_IDENTICAL_TURNS", "4")))
STALL_ACTION_REPEATS = max(
    STALL_IDENTICAL_TURNS, int(os.environ.get("STALL_ACTION_REPEATS", "12"))
)
STALL_ERROR_TURNS = max(2, int(os.environ.get("STALL_ERROR_TURNS", "4")))
STALL_NO_PROGRESS_TURNS = max(4, int(os.environ.get("STALL_NO_PROGRESS_TURNS", "5")))

# 退化回合不直接判失败,而是注入一句续写/推进的提示(有限次)。
CONTINUE_PROMPT = ("[系统] 你上一条回复被长度限制截断了。从断点处**继续**,不要重复前面的内容。")
NUDGE_PROMPT = "请继续:要么调用一个工具推进,要么给出你的最终文字答复。"

# 通用、薄的 base system,两个角色共用。领域方法在 skills/ 里,按需 read SKILL.md 获取。
BASE_SYSTEM = """\
你是一个自主的创作型 Agent,通过工具与文件系统、无头浏览器、网络交互来完成任务。

工作方式:
- 先理解任务。任务涉及某项专门能力时,`skills/` 下有对应的 SKILL.md —— 先 `read` 它,再按其方法执行(渐进式:先读 SKILL.md,需要时再读它引用的文件)。
- **自主推进**,不要向用户提问、不要中途停下等确认;自行补齐合理假设,做有品味的决定。
- 用工具**实际产出文件**,不要只在文字里描述。
- 全部完成后,用**一段简短文字**总结收尾 —— 这段文字就是你的最终输出。

可用技能:
- ppt-skill(`skills/ppt-skill/SKILL.md`):生成 HTML 幻灯片演示文稿(每页 1600×900,16:9)。
"""


def _read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _tool_action_signature(use):
    payload = {
        "name": str(getattr(use, "name", "")),
        "input": getattr(use, "input", {})
        if isinstance(getattr(use, "input", {}), dict)
        else {},
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _tool_result_text(result):
    content = result.get("content", "") if isinstance(result, dict) else result
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
        elif item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _tool_turn_signature(tool_uses, tool_results):
    payload = [
        {
            "action": _tool_action_signature(use),
            "result": _tool_result_text(result),
        }
        for use, result in zip(tool_uses, tool_results)
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _collapse_same_turn_tool_uses(content):
    """同一模型响应中的完全重复调用只执行一次，生图变体除外。"""
    blocks, calls, seen = [], [], set()
    collapsed = 0
    for block in content:
        if block.type != "tool_use":
            blocks.append(block)
            continue
        signature = _tool_action_signature(block)
        if block.name != "image_generate" and signature in seen:
            collapsed += 1
            continue
        seen.add(signature)
        blocks.append(block)
        calls.append(block)
    return blocks, calls, collapsed


_ERROR_RESULT_RE = re.compile(
    r"(?:错误：|崩溃:|\[exit_code=[1-9]\d*\]|traceback|"
    r"\b(?:failed|failure|timed?\s*out|timeout)\b|"
    r"\bhttp\s+(?:4\d\d|5\d\d)\b|\b(?:429|403)\b)",
    flags=re.I,
)


def _tool_error_signature(tool_uses, tool_results):
    errors = []
    for use, result in zip(tool_uses, tool_results):
        text = _tool_result_text(result)
        if not _ERROR_RESULT_RE.search(text):
            continue
        normalized = re.sub(r"https?://\S+", "<url>", text[:1200], flags=re.I)
        normalized = re.sub(r"\b[0-9a-f]{12,}\b|\b\d{5,}\b", "<id>", normalized, flags=re.I)
        normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
        errors.append(f"{getattr(use, 'name', '')}:{normalized}")
    if not errors:
        return ""
    return hashlib.sha256("\n".join(sorted(errors)).encode("utf-8")).hexdigest()


class _ProgressGuard:
    """检测重复执行与无产物进展；先警告一次，再停止当前 Agent。"""

    _EXCLUDED_ROOTS = {"_trace", "skills", "inputs"}

    def __init__(self, workspace):
        self.root = Path(workspace)
        self.file_cache = {}
        self.workspace_signature = self._workspace_signature()
        self.seen_actions = set()
        self.action_counts = Counter()
        self.last_turn_signature = ""
        self.identical_turns = 0
        self.last_error_signature = ""
        self.error_turns = 0
        self.no_progress_turns = 0
        self.warned = set()

    def _workspace_signature(self):
        rows = []
        if not self.root.is_dir():
            return ""
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root)
            if relative.parts and relative.parts[0] in self._EXCLUDED_ROOTS:
                continue
            if "__pycache__" in relative.parts or path.suffix == ".pyc":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = relative.as_posix()
            marker = (stat.st_size, stat.st_mtime_ns)
            cached = self.file_cache.get(key)
            if cached and cached[:2] == marker and stat.st_size > 1_000_000:
                digest = cached[2]
            else:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                self.file_cache[key] = (stat.st_size, stat.st_mtime_ns, digest)
            rows.append((key, digest))
        return hashlib.sha256(_canonical_json(rows).encode("utf-8")).hexdigest()

    def observe(self, tool_uses, tool_results):
        actions = [_tool_action_signature(use) for use in tool_uses]
        novel_action = any(action not in self.seen_actions for action in actions)
        self.seen_actions.update(actions)
        self.action_counts.update(actions)

        turn_signature = _tool_turn_signature(tool_uses, tool_results)
        if turn_signature == self.last_turn_signature:
            self.identical_turns += 1
        else:
            self.last_turn_signature = turn_signature
            self.identical_turns = 1

        error_signature = _tool_error_signature(tool_uses, tool_results)
        if error_signature and error_signature == self.last_error_signature:
            self.error_turns += 1
        elif error_signature:
            self.last_error_signature = error_signature
            self.error_turns = 1
        else:
            self.last_error_signature = ""
            self.error_turns = 0

        new_workspace_signature = self._workspace_signature()
        workspace_changed = new_workspace_signature != self.workspace_signature
        self.workspace_signature = new_workspace_signature
        if workspace_changed:
            self.action_counts.clear()
            self.action_counts.update(actions)
            self.identical_turns = 1
            self.error_turns = 1 if error_signature else 0
            self.warned.clear()
        if workspace_changed or novel_action:
            self.no_progress_turns = 0
        else:
            self.no_progress_turns += 1

        repeated_action = max(
            (self.action_counts[action] for action in actions), default=0
        )
        checks = (
            ("identical", self.identical_turns, STALL_IDENTICAL_TURNS, "连续重复完全相同的工具调用与结果"),
            ("action", repeated_action, STALL_ACTION_REPEATS, "同一工具参数被过度重复"),
            ("error", self.error_turns, STALL_ERROR_TURNS, "连续收到同一类不可恢复错误"),
            ("progress", self.no_progress_turns, STALL_NO_PROGRESS_TURNS, "工具行为与工作区产物均无新进展"),
        )
        for _key, value, limit, description in checks:
            if value >= limit:
                return "stop", f"{description}（{value}/{limit}）"
        for key, value, limit, description in checks:
            if value >= max(2, limit - 1) and key not in self.warned:
                self.warned.add(key)
                return "warn", f"{description}（{value}/{limit}）"
        return "ok", ""


def _subagent_task(slide, note=None):
    n = f"{int(slide):02d}"
    t = (f"按 ppt-skill 写这个 deck 的第 {slide} 页。先 `read skills/ppt-skill/SKILL.md` 弄清写页要求和"
         f"自检清单,然后 `read plan/deck.md`、`plan/slide_{n}.md`、`base.css`,写 `slides/slide_{n}.html`,"
         f"`render` 它,用 `vision_analyze` 看截图按清单自检,最多 3 轮 edit→render→看图 修到通过。"
         f"只动 `slides/slide_{n}.html`。最后用一段文字总结:这一页画了什么、状态(通过 / 还剩哪些问题)、"
         f"截图路径 `renders/slide_{n}.png`。")
    if note:
        t += f"\n\n编排器的额外修改要求:{note}"
    return t


class Agent:
    """通用单 Agent 循环(Claude + 工具);Agent 对象 = 状态,循环逻辑在下面的 run_loop。

    编排器和 subagent 共用这个类,区别只在:工具集(orchestrator_tools vs subagent_tools)、
    初始任务、以及编排器额外注册了 delegate_task。编排器和它的 subagent **共享同一个工作区
    ws(= run_dir)**(它们协作产出同一套 deck),但各写各的轨迹 sub_dir。"""

    def __init__(self, role, sid, ws, sub_dir, tools_schema, config,
                 initial_user, label, extra_tools=None):
        self.role = role
        self.sid = sid
        self.ws = os.path.abspath(ws)                 # 工作区 = run_dir(plan/ base.css slides/ renders/ assets/)
        trace_namespace = str((config or {}).get("trace_namespace") or "").strip("/\\")
        self.sub_dir = os.path.join(
            self.ws, "_trace", trace_namespace, sub_dir
        ) if trace_namespace else os.path.join(self.ws, "_trace", sub_dir)
        os.makedirs(self.sub_dir, exist_ok=True)
        os.makedirs(os.path.join(self.sub_dir, "images"), exist_ok=True)
        self.initial_user = initial_user
        self.label = label
        self.extra_tools = extra_tools or {}

        self.tools = tools_schema
        self.system = BASE_SYSTEM

        # —— tools.py 依赖的上下文字段 ——
        self.cfg = config or {}
        self.generation_preferences = dict(self.cfg.get("_generation_preferences") or {})
        if self.generation_preferences:
            settings = json.dumps(self.generation_preferences, ensure_ascii=False, sort_keys=True)
            self.system += (
                "\n\nRuntime presentation settings (authoritative; do not present them as user prose):\n"
                f"{settings}\n"
                "Apply these settings to planning and production. The original user request remains unchanged."
            )
        self.serper = os.environ.get("SERPER_API_KEY")
        self.serper_base = self.cfg.get(
            "serper_base_url",
            os.environ.get("SERPER_BASE_URL", "https://google.serper.dev"),
        ).rstrip("/")
        self.img_base = self.cfg.get("openai_base_url",
                                     os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")).rstrip("/")
        self.img_key = os.environ.get("IMAGE_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        self.image_model = self.cfg.get("image_model", os.environ.get("IMAGE_MODEL", "gpt-image-2"))
        self.img_n = 0

        # —— 运行状态 ——
        self.shot_by_tcid = {}      # tool_call_id -> 该轨迹内 images/ 下的快照相对路径
        self.view_n = 0             # 看过几张图(给快照按查看顺序编号 view_NN.png)
        self.last_shot = None       # 最近一次成功 render 的工作区相对路径
        self.n_renders = 0
        self.n_vision_calls = 0     # 成功获得真实像素/视觉分析的调用数
        self.final_text = ""
        self.exit_reason = None
        self.worker_recs = []       # 仅编排器:每个 subagent 的小结
        self._spawn_count = {}
        self._spawn_lock = threading.Lock()
        # 父级并发闸:所有 delegate_task 调用共用一个信号量,真正的子 agent 并发上限由它统一卡。
        # 这样即便同一回合有多个 delegate_task 调用(被 _run_tools 并行执行),总并发也不会超上限。
        self._child_sem = threading.Semaphore(MAX_CONCURRENT_CHILDREN)
        self._delegate_depth = 0

        # —— 模型客户端 ——
        self.model = self.cfg.get("model", os.environ.get("MODEL", "claude-opus-4-7"))
        self.a_base = self.cfg.get("anthropic_base_url",
                                   os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"))
        self.max_turns = int(self.cfg.get("max_turns", 120))
        self.max_tokens = int(self.cfg.get("max_tokens", 32000))
        self.requested_thinking = os.environ.get(
            "STUDIO_REQUESTED_THINKING", os.environ.get("THINKING", "0")
        ) != "0"
        self.effective_thinking = os.environ.get(
            "STUDIO_EFFECTIVE_THINKING", os.environ.get("THINKING", "0")
        ) != "0"
        self.thinking_transport = os.environ.get("STUDIO_THINKING_TRANSPORT", "")
        self.thinking = self.effective_thinking
        self.think_effort = os.environ.get("THINK_EFFORT", "high")
        # 后端切换:MODEL_BACKEND=openai 时驱动部署的 student 模型(vLLM/OpenAI 兼容),
        # 鸭子类型成 anthropic 客户端,run_loop / 工具 / 渲染 / 验收全不改。teacher 路径默认不变。
        if os.environ.get("MODEL_BACKEND", "").lower() == "openai":
            import openai_backend
            self.model = self.cfg.get("model", os.environ.get("STUDENT_MODEL", self.model))
            self.thinking = False   # OpenAI 兼容模型由请求体 transport 控制，不传 Anthropic 参数
            student_base_url = os.environ.get("STUDENT_BASE_URL", "").strip()
            if not student_base_url:
                raise RuntimeError(
                    "MODEL_BACKEND=openai 时必须配置 STUDENT_BASE_URL"
                )
            self.client = openai_backend.OpenAIShim(
                base=student_base_url,
                model=self.model, key=os.environ.get("STUDENT_API_KEY", "EMPTY"))
        else:
            self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=self.a_base)

        if any(t.get("name") == "delegate_task" for t in self.tools):
            self.extra_tools.setdefault("delegate_task", lambda **a: delegate_task(self, **a))

    def log(self, m):
        print(f"[{self.sid}/{self.label}] {m}", flush=True)

    # —— 沙箱:写/渲染限定在 ws 内;读还可读只读的 skills/ 树 ——
    def safe(self, path):
        p = os.path.normpath(os.path.join(self.ws, path))
        if p != os.path.normpath(self.ws) and not p.startswith(os.path.normpath(self.ws) + os.sep):
            raise ValueError(f"路径越出工作区: {path}")
        return p

    def writable(self, path):
        """写入策略:编排器**不许写/改 `slides/` 下的页面 HTML**(必须重新委派 subagent)。
        跨页统一的视觉改动应改 `base.css`;跨页内容改动应带 note 重新委派受影响的页。"""
        if self.role == "orchestrator":
            p = os.path.normpath(os.path.join(self.ws, path))
            slides_dir = os.path.normpath(os.path.join(self.ws, "slides"))
            if p == slides_dir or p.startswith(slides_dir + os.sep):
                return False
        return True

    def read_path(self, path):
        if path == "skills":
            return self.safe(path)
        if path == "skills/ppt-skill" or path.startswith("skills/ppt-skill/"):
            rel = path[len("skills/ppt-skill"):].lstrip("/")
            p = os.path.normpath(os.path.join(PPT_SKILL_DIR, rel))
            sk = os.path.normpath(PPT_SKILL_DIR)
            if p != sk and not p.startswith(sk + os.sep):   # 带分隔符,防 skills_evil/ 这类同前缀越界
                raise ValueError("路径越出 ppt-skill")
            return p
        if path.startswith("skills/"):
            p = os.path.normpath(os.path.join(SKILLS_DIR, path[len("skills/"):]))
            sk = os.path.normpath(SKILLS_DIR)
            if p != sk and not p.startswith(sk + os.sep):
                raise ValueError("路径越出 skills")
            return p
        return self.safe(path)

    # —— 记账 ——
    def snapshot_inputs(self):
        tool_names = [str(tool.get("name") or "") for tool in self.tools]
        with open(os.path.join(self.sub_dir, "system_prompt.md"), "w", encoding="utf-8") as f:
            f.write(self.system)
        _write_json(os.path.join(self.sub_dir, "tools.json"), self.tools)
        _write_json(os.path.join(self.sub_dir, "config.json"), {
            "role": self.role, "sample_id": self.sid, "label": self.label,
            "task": self.initial_user, "model": self.model, "anthropic_base_url": self.a_base,
            "image_model": self.image_model, "max_tokens": self.max_tokens, "max_turns": self.max_turns,
            "serper": bool(self.serper), "pid": os.getpid(),
            "thinking": (
                {"type": self.thinking_transport, "enabled": self.effective_thinking}
                if self.thinking_transport else
                ({"type": "adaptive", "display": "summarized", "effort": self.think_effort} if self.effective_thinking else False)
            ),
            "requested_thinking": self.requested_thinking,
            "effective_thinking": self.effective_thinking,
            "thinking_transport": self.thinking_transport,
            "generation_preferences": self.generation_preferences,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tools": tool_names,
            "vision_available": "vision_analyze" in tool_names,
        })

    @staticmethod
    def blocks_to_dicts(content):
        out = []
        for b in content:
            if b.type == "text":
                out.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif b.type == "thinking":
                blk = {"type": "thinking", "thinking": b.thinking}
                sig = getattr(b, "signature", None)
                if sig:
                    blk["signature"] = sig
                out.append(blk)
            elif b.type == "redacted_thinking":
                out.append({"type": "redacted_thinking", "data": b.data})
        return out

    def clean(self, m):
        """把 messages 里的 tool_result 图像块换成轻量的 {type:image, shot:<快照路径>},
        避免把几 MB 的 base64 灌进 messages.json(真正的像素已快照到 images/)。"""
        if not isinstance(m.get("content"), list):
            return m
        c = []
        for x in m["content"]:
            if isinstance(x, dict) and x.get("type") == "tool_result" and isinstance(x.get("content"), list):
                shot = self.shot_by_tcid.get(x.get("tool_use_id"))
                newc = [({"type": "image", "shot": shot}
                         if isinstance(y, dict) and y.get("type") == "image" else y)
                        for y in x["content"]]
                c.append({**x, "content": newc})
            elif isinstance(x, dict) and x.get("type") == "image":
                src = x.get("source") or {}
                c.append({"type": "image", "source": {
                    "type": src.get("type", "base64"),
                    "media_type": src.get("media_type", "image/png"),
                    "data": "<omitted>",
                }})
            else:
                c.append(x)
        return {**m, "content": c}

    def run(self):
        return run_loop(self)


# ===================== 循环(自由函数,操作一个 agent) =====================

# prompt cache(PROMPT_CACHE=0 关闭):TokenHub 的 claude 多为 aws bedrock 渠道,top-level
# cache_control 不生效,只能在 system/content **block 上**挂;openai shim 的 to_openai_messages
# 会把 block 拍平重建,cache_control 不会漏到严格 openai 端点;vLLM 学生的 anthropic_proxy 显式忽略。
_PROMPT_CACHE = os.environ.get("PROMPT_CACHE", "1").strip().lower() not in {"0", "false", "no", "off"}
_EPHEMERAL = {"type": "ephemeral"}
_CACHEABLE_BLOCK_TYPES = {"text", "image", "tool_result", "document"}


def _cache_annotated(messages):
    """拷贝式地在最后一条消息的最后一个 block 挂 cache_control,不改调用方的 messages
    (recorder 收集的轨迹因此不带缓存标记)。多轮 tool 循环每轮重发全量历史:上一轮写的
    缓存本轮作为前缀命中,增量续写。每次请求只带 system + 这里共 2 个 breakpoint(上限 4)。"""
    if not messages:
        return messages
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return messages
        blocks = [{"type": "text", "text": content, "cache_control": _EPHEMERAL}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict) \
            and content[-1].get("type") in _CACHEABLE_BLOCK_TYPES:
        blocks = content[:-1] + [{**content[-1], "cache_control": _EPHEMERAL}]
    else:
        return messages
    return messages[:-1] + [{**last, "content": blocks}]


def _model_call(agent, messages, with_tools=True):
    """一次模型调用,带有限的传输层重试。with_tools=False 时去掉工具,逼模型只能出文字。"""
    system = agent.system
    if _PROMPT_CACHE:
        if isinstance(system, str) and system:
            # [tools + system] 是跨轮完全稳定的前缀,挂在 system block 上一并缓存
            system = [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]
        messages = _cache_annotated(messages)
    kwargs = dict(model=agent.model, max_tokens=agent.max_tokens, system=system, messages=messages)
    if with_tools:
        kwargs["tools"] = agent.tools
    if agent.thinking:
        # Opus 4.7/4.8:effort 属于 output_config(放进 thinking 里会被静默忽略);
        # display 默认 "omitted" → thinking 文本为空,必须显式 "summarized" 才能把(摘要版)推理写进轨迹。
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        kwargs["output_config"] = {"effort": agent.think_effort}
    for attempt in range(4):
        try:
            return agent.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            # 400 是请求本身的问题,重试不会变好;立刻暴露真实原因,别白等 30s。
            agent.log(f"[api 400 不重试] {str(e)[:300]}")
            return None
        except Exception as e:
            agent.log(f"[api err {attempt}] {str(e)[:160]}")
            time.sleep(3 * (attempt + 1))
    return None


def _exec_one_tool(agent, tu):
    """执行单个 tool_use,返回它的 tool_result(含 render 记账 / 图像快照 / is_error)。
    render 记账与图像快照会改 agent 状态(n_renders/last_shot/view_n/shot_by_tcid),所以这些工具只在
    _run_tools 里**顺序**调;delegate_task 不碰这些 parent 状态(子 agent 各写各的),可安全并行。"""
    args = tu.input if isinstance(tu.input, dict) else {}
    err = False
    try:
        res = tools.dispatch(agent, tu.name, args)
    except Exception as e:
        res = f"{tu.name} 崩溃: {e}"
        err = True
    if tu.name == "vision_analyze":
        vision_ok = (
            isinstance(res, dict)
            and (res.get("b_vision") or bool(res.get("image_b64")))
        ) or (
            isinstance(res, str)
            and res.startswith("[gemini 看图分析")
        )
        if vision_ok:
            agent.n_vision_calls += 1
    # 渲染现在是 subagent 用 bash 跑 render.py(成功时 stdout 是 PNG 路径);据此记账 last_shot/n_renders。
    if tu.name == "bash" and "render.py" in str(args.get("command", "")) \
            and isinstance(res, str) and not res.startswith("bash 错误"):
        agent.n_renders += 1
        line = res.strip().splitlines()[-1].strip() if res.strip() else ""
        if line.endswith(".png"):
            try:
                agent.last_shot = os.path.relpath(line, agent.ws) if os.path.isabs(line) else line
            except Exception:
                agent.last_shot = line
    if isinstance(res, dict) and "image_b64" in res:
        tcid = tu.id
        agent.view_n += 1               # 按查看顺序编号,文件名清爽且有序
        rel = os.path.join("images", f"view_{agent.view_n:02d}.png")
        try:
            with open(os.path.join(agent.sub_dir, rel), "wb") as f:
                f.write(base64.b64decode(res["image_b64"]))
            agent.shot_by_tcid[tcid] = rel
        except Exception as e:
            agent.log(f"WARN: 快照图像失败 {e}")
        return {"type": "tool_result", "tool_use_id": tcid, "content": [
            {"type": "text", "text": res.get("summary", "")},
            {"type": "image", "source": {"type": "base64", "media_type": res.get("media_type", "image/png"),
                                         "data": res["image_b64"]}}]}
    tr = {"type": "tool_result", "tool_use_id": tu.id, "content": str(res)[:8000]}
    if err:                       # 工具抛错(如 read 不存在的文件)→ 标 is_error,
        tr["is_error"] = True     # to_openai 据此把该 tool 消息 success 置 False(否则失败被误标成功)
    return tr


def _run_tools(agent, tool_uses, turn, tool_log):
    """执行一个回合的所有 tool_use;每个 tool_use **一定**配一个 tool_result(即使失败)。
    同一回合里的**多个 `delegate_task` 并行执行**(线程安全:_spawn_lock + 父级 _child_sem 统一限并发),
    其余工具顺序执行(它们会改 agent 的快照/渲染状态)。结果按原 tool_use 顺序回填。"""
    for tu in tool_uses:
        args = tu.input if isinstance(tu.input, dict) else {}
        agent.log(f"🔧 {tu.name}({json.dumps(args, ensure_ascii=False)[:130]})")
        tool_log.append({"turn": turn, "name": tu.name, "args": args})
    results = [None] * len(tool_uses)
    deleg = [i for i, tu in enumerate(tool_uses) if tu.name == "delegate_task"]
    for i, tu in enumerate(tool_uses):                 # 非 delegate:顺序(含会改 agent 状态的记账)
        if i not in deleg:
            results[i] = _exec_one_tool(agent, tu)
    if len(deleg) <= 1:                                # 0 / 1 个 delegate:顺序,行为与旧版一致
        for i in deleg:
            results[i] = _exec_one_tool(agent, tool_uses[i])
    else:                                              # 多个 delegate:并行(真正并发由父级 _child_sem 统一卡)
        with cf.ThreadPoolExecutor(max_workers=len(deleg), thread_name_prefix="deleg") as ex:
            futs = {ex.submit(_exec_one_tool, agent, tool_uses[i]): i for i in deleg}
            for f in cf.as_completed(futs):
                results[futs[f]] = f.result()
    return results


def run_loop(agent):
    """ReAct 循环:模型 -> 工具 -> 模型,直到自然出文字收尾、有限自愈(空/截断)、或到 max_turns
    (再做一次去工具的强制总结,保证轨迹尽量以真 assistant 文字结尾)。写原始轨迹;返回 finished_clean。"""
    agent.snapshot_inputs()
    messages = [{"role": "user", "content": agent.initial_user}]
    tool_log = []
    heals = 0
    progress_guard = _ProgressGuard(agent.ws)
    for turn in range(agent.max_turns):
        resp = _model_call(agent, messages, with_tools=True)
        if resp is None:
            agent.exit_reason = "api_failed"
            agent.log("API 连续失败,放弃")
            break

        response_blocks, tool_uses, collapsed_calls = _collapse_same_turn_tool_uses(
            resp.content
        )
        if collapsed_calls:
            agent.log(
                f"[{turn}] 同回合去重:跳过 {collapsed_calls} 个完全重复工具调用"
            )
        messages.append(
            {"role": "assistant", "content": agent.blocks_to_dicts(response_blocks)}
        )
        turn_text = ""
        for b in response_blocks:
            if b.type == "thinking" and b.thinking.strip():
                agent.log(f"[{turn}] 🧠 {b.thinking.strip()[:140]}")
            if b.type == "text" and b.text.strip():
                agent.log(f"[{turn}] 💬 {b.text.strip()[:180]}")
                turn_text = b.text.strip()
                agent.final_text = turn_text
        if not tool_uses:
            # 没有工具调用:只有真正的 assistant 文字才算干净收尾;空/截断回合做有限自愈。
            if turn_text:
                agent.exit_reason = "text_response"
                agent.log(f"完成于回合 {turn}(stop={resp.stop_reason})")
                break
            if heals < MAX_HEALS:
                heals += 1
                truncated = resp.stop_reason == "max_tokens"
                agent.log(f"[{turn}] {'被截断' if truncated else '空'}回复,继续({heals}/{MAX_HEALS})")
                messages.append({"role": "user", "content": CONTINUE_PROMPT if truncated else NUDGE_PROMPT})
                continue
            agent.exit_reason = "empty_giveup"
            agent.log(f"连续退化(空)回复,回合 {turn} 放弃")
            break

        heals = 0
        tool_results = _run_tools(agent, tool_uses, turn, tool_log)
        progress_action, progress_reason = progress_guard.observe(
            tool_uses, tool_results
        )
        if progress_action == "warn":
            tool_results.append({
                "type": "text",
                "text": (
                    "[系统] 检测到可能停滞："
                    f"{progress_reason}。停止重复当前路径；换一种方法继续，"
                    "若错误不可恢复则明确说明。"
                ),
            })
        messages.append({"role": "user", "content": tool_results})
        if progress_action == "stop":
            agent.exit_reason = "stalled_repetition"
            agent.log(f"[{turn}] 循环卡死保护触发:{progress_reason}")
            break
    else:
        agent.exit_reason = "max_turns"
        agent.log(f"到达 max_turns({agent.max_turns})")

    # 优雅收尾:不只是 max_turns；重复停滞或空回复放弃也可能恰好停在
    # vision_analyze 的 tool_result 上。这时如果不强制一次可见总结，页面历史只能
    # 看到“检查项”而永远没有“检查结果”。收尾文字仅用于保存诚实轨迹，
    # 不把未自然收尾的子 Agent 误标为 clean。
    summarizable_exits = {"max_turns", "stalled_repetition", "empty_giveup"}
    if agent.exit_reason in summarizable_exits and messages and messages[-1]["role"] == "user":
        original_exit = agent.exit_reason
        agent.log(f"{original_exit} 后强制一次去工具的可见总结")
        resp = _model_call(agent, messages, with_tools=False)
        if resp is not None:
            txt = next((b.text.strip() for b in resp.content if b.type == "text" and b.text.strip()), "")
            if txt:
                messages.append({"role": "assistant", "content": agent.blocks_to_dicts(resp.content)})
                agent.final_text = txt
                agent.exit_reason = f"{original_exit}_summarized"

    finished_clean = agent.exit_reason == "text_response"
    _write_json(os.path.join(agent.sub_dir, "messages.json"), [agent.clean(m) for m in messages])
    _write_json(os.path.join(agent.sub_dir, "tool_log.json"), tool_log)
    agent.log(f"轨迹已写: turns={len(tool_log)} renders={agent.n_renders} exit={agent.exit_reason}")
    return finished_clean


# ============= 委派 subagent(自由函数,操作一个 agent) =============

_STANDARD_ROLE_TOOLSETS = {
    "research": ["file", "web"],
    "material": ["file", "terminal", "vision"],
    "image": ["file", "terminal", "web", "image_gen", "vision"],
    "slide": ["file", "terminal", "vision"],
    "review": ["vision"],
    "player": ["file", "terminal"],
}


def _standard_label_from_goal(goal):
    """恢复 sn-ppt-standard goal 中省略的结构化角色名。"""
    text = str(goal or "")
    marker = re.search(
        r"\[(research|material|image|slide|player)(?:[_-](\d{1,3}))?\]|\[(review)\]",
        text,
        re.I,
    )
    if marker:
        role = (marker.group(1) or marker.group(3)).lower()
        return f"{role}_{int(marker.group(2)):02d}" if marker.group(2) else role
    path = re.search(r"subagents/(research|material|image|slide|review)\.md", text, re.I)
    if path:
        role = path.group(1).lower()
        page = re.search(r"(?:第\s*|slide[_ -]?)(\d{1,3})\s*页?", text, re.I)
        return f"{role}_{int(page.group(1)):02d}" if page and role != "review" else role
    low = text.strip().lower()
    if low.startswith("review"):
        return "review"
    if low.startswith("player") or "build_player.py" in low:
        return "player"
    return None


def _normalize_task(t, profile=""):
    """把一条 delegate task 归一成 {goal, context, toolsets, role, label}(对齐 hermes)。

    兼容旧形态 `{slide, note}`(SKILL.md 迁移到 goal 派发前的过渡):由 slide 号合成 slide-writer
    的 goal、默认 toolsets 与 label。新形态直接用 goal/context/toolsets/role/label。"""
    if not isinstance(t, dict):
        t = {"goal": str(t)}
    if "slide" in t:                                   # 旧形态:{slide, note?/goal?}
        slide = t.get("slide")
        note = t.get("note") or t.get("goal")
        return {"goal": _subagent_task(slide, note), "context": t.get("context", ""),
                "toolsets": list(t.get("toolsets") or tools.SLIDE_WRITER_TOOLSETS),
                "role": t.get("role", "leaf"),
                "label": t.get("label") or (f"slide_{int(slide):02d}" if slide is not None else None)}
    if profile == "sense-present-standard":
        goal = str(t.get("goal") or "")
        label = str(t.get("label") or "").strip() or _standard_label_from_goal(goal)
        toolsets = list(dict.fromkeys(t.get("toolsets") or []))
        kind = str(label or "").split("_", 1)[0].lower()
        if kind in {"slide", "image", "review"}:
            role_path = f"skills/sn-ppt-standard/subagents/{kind}.md"
            goal = (
                f"开工先用 read 完整读取 `{role_path}`；这是唯一角色卡路径，"
                "不要搜索文件系统寻找副本。\n\n" + goal
            )
            if kind in {"slide", "review"}:
                goal = (
                    "Vision 记录要求：每批 `vision_analyze` 返回后，下一轮必须先在可见文本中"
                    "逐页写出 `slide_NN: <检查结果>`，再发起任何新工具调用。"
                    "不得连续发起下一批 Vision 而不留下可见判断。\n\n" + goal
                )
        elif kind == "player":
            goal = (
                "构建脚本的唯一有效路径是 `skills/sn-ppt-standard/scripts/build_player.py`；"
                "直接使用该工作区相对路径，不要搜索文件系统或使用猜测的绝对路径。\n\n"
                + goal
            )
        for required in _STANDARD_ROLE_TOOLSETS.get(kind, tools.SLIDE_WRITER_TOOLSETS):
            if required not in toolsets:
                toolsets.append(required)
        return {"goal": goal, "context": t.get("context", ""),
                "toolsets": toolsets, "role": t.get("role", "leaf"), "label": label}
    return {"goal": t.get("goal", ""), "context": t.get("context", ""),
            "toolsets": list(t.get("toolsets") or tools.SLIDE_WRITER_TOOLSETS),
            "role": t.get("role", "leaf"), "label": t.get("label")}


def _build_child(parent, task):
    """从 parent 构造(不运行)一个子 agent。`task` = 归一后的 {goal, context, toolsets, role, label}。
    每次尝试拿到唯一的 sub_dir + label(重做 → `<label>_r2`…),重做不覆盖上次的轨迹/快照。"""
    base = task.get("label")
    with parent._spawn_lock:
        if not base:
            parent._child_seq = getattr(parent, "_child_seq", 0) + 1
            base = f"child_{parent._child_seq:02d}"
        c = parent._spawn_count.get(base, 0) + 1
        parent._spawn_count[base] = c
    name = base + ("" if c == 1 else f"_r{c}")

    toolsets = list(task.get("toolsets") or tools.SLIDE_WRITER_TOOLSETS)
    # role=orchestrator 且深度还允许再派时,才补 delegation 能力(否则给了也会被深度上限挡)。
    if task.get("role") == "orchestrator" and parent._delegate_depth + 1 < MAX_SPAWN_DEPTH \
            and "delegation" not in toolsets:
        toolsets = toolsets + ["delegation"]
    schema = tools.resolve_toolsets(toolsets)
    # 基础工具(read)默认并入:若所选 toolsets 未包含,则补上(BASE_TOOL_NAMES 是工具名,不是 toolset)。
    have = {s["name"] for s in schema}
    schema = [tools.SCHEMAS[n] for n in tools.BASE_TOOL_NAMES if n not in have] + schema

    context = task.get("context") or ""
    initial = task["goal"] + (f"\n\n背景:\n{context}" if context else "")
    initial += (
        "\n\n用户界面语言要求：所有阶段进展和最终回复的第一段必须使用简洁中文，"
        "不要先输出英文检查结论。文件路径、代码标识和结构化交付字段可以保留英文；"
        "详细技术记录可放在中文摘要之后。"
    )
    child_config = parent.cfg
    child_max_turns = int(parent.cfg.get("child_max_turns") or 0)
    if child_max_turns:
        child_config = dict(parent.cfg)
        child_config["max_turns"] = child_max_turns
    child = Agent(role="subagent", sid=parent.sid, ws=parent.ws,
                  sub_dir=f"subagents/{name}", tools_schema=schema,
                  config=child_config, initial_user=initial, label=name)
    child._delegate_depth = parent._delegate_depth + 1
    return child, name


def _run_child(parent, task, ticket):
    """构造 + 跑完一个子 agent,把小结挂到 parent,返回紧凑结果(不让子轨迹/图像穿透到 parent)。

    `ticket` 是父子共享的小状态:父超时放弃时会置 `abandoned`,届时这个仍在跑的线程结束后
    **不再写 worker_recs**(父已记过一条 timeout),避免迟到的脏写/竞态翻转验收。worker_recs 的
    读写一律走 `parent._spawn_lock`。"""
    child, name = _build_child(parent, task)
    ticket["label"] = name
    with parent._child_sem:                  # 父级并发闸:跨多个并发的 delegate_task 调用统一限子 agent 并发
        fin = child.run()
    rec = {"label": name, "clean": fin, "renders": child.n_renders,
           "vision_calls": child.n_vision_calls,
           "shot": child.last_shot, "exit_reason": child.exit_reason,
           "summary": child.final_text or ""}
    with parent._spawn_lock:
        if not ticket.get("abandoned"):
            parent.worker_recs.append(rec)
            ticket["recorded"] = True   # 告知超时路径:本子 agent 已有真实记录,别再补 timeout 记录
    return _child_delivery(parent, child, name, fin)


def _child_delivery(parent, child, name, clean):
    """Return complete child text and point at every durable handoff artifact."""
    summary = child.final_text or ""
    summary_abs = os.path.join(child.sub_dir, "summary.md")
    if not os.path.isfile(summary_abs):
        with open(summary_abs, "w", encoding="utf-8") as handle:
            handle.write(summary + ("" if summary.endswith("\n") else "\n"))
    summary_path = os.path.relpath(summary_abs, parent.ws).replace(os.sep, "/")
    artifacts = []
    image_manifest = f"assets/image-manifests/{name}.json"
    if os.path.isfile(os.path.join(parent.ws, image_manifest)):
        artifacts.append(image_manifest)
    return {
        "label": name,
        "status": "ok" if clean else "issues",
        "renders": child.n_renders,
        "vision_calls": child.n_vision_calls,
        "shot": child.last_shot,
        "summary": summary,
        "summary_path": summary_path,
        "summary_chars": len(summary),
        "summary_truncated": False,
        "artifacts": artifacts,
    }


def _mark_worker_superseded(parent, label):
    """失败尝试被自动重派后，不再让旧尝试污染最终验收。"""
    with parent._spawn_lock:
        for record in reversed(parent.worker_recs):
            if record.get("label") == label and not record.get("superseded"):
                record["superseded"] = True
                return


def delegate_task(parent, goal=None, context=None, toolsets=None, role=None,
                  label=None, tasks=None, **_extra):
    """并行起一批子 agent 跑任务,返回 `{"results":[...]}` 的 JSON 字符串(对齐 hermes)。

    两种形态:顶层单个 `{goal,context?,toolsets?,role?,label?}`,或 `tasks` 数组批量。子 agent
    "类型"由 toolsets + goal 在调用时拼出。每次调用用一个**本地** ThreadPoolExecutor(用完即关,
    不留模块级全局池,避免跨 sample/调用串台)。单个子 agent 有硬超时:超时记一条 `clean=False`
    (让 `_accept` 拒收)+ 标记 abandoned(迟到线程丢弃自己的记录)。"""
    # 小模型(如 9B)有时把 tasks 数组当成 JSON 字符串塞进来(arguments 里 "tasks":"[{...}]"),
    # 而非真数组 → 不宽容解析的话 _normalize_task 拿不到 goal、回错,模型反复重发同样格式陷入死循环。
    # 这里把 str 形态的 tasks/goal 宽容还原(Opus 传真数组,isinstance 判 False 不受影响)。
    if isinstance(tasks, str):
        try:
            _p = json.loads(tasks)
            tasks = _p if isinstance(_p, list) else ([_p] if isinstance(_p, dict) else [{"goal": tasks}])
        except Exception:
            tasks = [{"goal": tasks}]
    elif isinstance(tasks, dict):
        tasks = [tasks]
    if tasks is None:
        if goal or label or toolsets:
            tasks = [{"goal": goal, "context": context, "toolsets": toolsets,
                      "role": role, "label": label}]
        else:
            tasks = []
    if not isinstance(tasks, list) or not tasks:
        return json.dumps({"error": "delegate_task 需要 goal 或非空 tasks[]"}, ensure_ascii=False)
    if parent._delegate_depth >= MAX_SPAWN_DEPTH:
        return json.dumps({"error": "已到委派深度上限(叶子子 agent 不能再委派)"}, ensure_ascii=False)

    profile = str(getattr(parent, "cfg", {}).get("harness_profile") or "")
    norm = [_normalize_task(t, profile) for t in tasks]

    def run_one(nt, ticket):
        last_result = None
        for attempt in range(1, CHILD_MAX_ATTEMPTS + 1):
            attempt_task = dict(nt)
            if attempt > 1:
                previous = str((last_result or {}).get("summary") or "")[-800:]
                retry_note = (
                    f"自动重派：上一尝试未完成（第 {attempt - 1} 次）。"
                    "读取工作区已有产物，只修复未完成部分；不要重做已通过内容。"
                )
                if previous:
                    retry_note += f"\n上一尝试末尾：{previous}"
                context = str(attempt_task.get("context") or "").strip()
                attempt_task["context"] = (
                    f"{context}\n\n{retry_note}" if context else retry_note
                )
                parent.log(
                    f"子 agent {nt.get('label') or 'child'} 失败后自动重派 "
                    f"({attempt}/{CHILD_MAX_ATTEMPTS})"
                )
            try:
                result = _run_child(parent, attempt_task, ticket)
            except Exception as exc:
                message = f"子 agent 崩溃: {exc}"
                result = {
                    "label": nt.get("label") or "child",
                    "status": "error",
                    "renders": 0,
                    "shot": None,
                    "summary": message,
                    "summary_path": None,
                    "summary_chars": len(message),
                    "summary_truncated": False,
                    "artifacts": [],
                }
            result["attempt"] = attempt
            result["max_attempts"] = CHILD_MAX_ATTEMPTS
            if result.get("status") == "ok":
                return result
            last_result = result
            if attempt < CHILD_MAX_ATTEMPTS:
                _mark_worker_superseded(parent, str(result.get("label") or ""))

        # Exceptions before Agent construction do not create a worker record;
        # persist one final failure so acceptance cannot silently pass it.
        final_label = str((last_result or {}).get("label") or nt.get("label") or "child")
        with parent._spawn_lock:
            if not any(
                record.get("label") == final_label
                and not record.get("superseded")
                for record in parent.worker_recs
            ):
                parent.worker_recs.append({
                    "label": final_label,
                    "clean": False,
                    "renders": 0,
                    "shot": None,
                    "exit_reason": "retry_exhausted",
                })
        return last_result

    ex = cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN, thread_name_prefix="child")
    try:
        triples = []
        for nt in norm:
            ticket = {"abandoned": False, "label": nt.get("label")}
            triples.append((nt, ticket, ex.submit(run_one, nt, ticket)))
        out = []
        for nt, ticket, f in triples:
            # 含 image_gen 的子 agent 给更长超时(配图天然慢);其余沿用 WORKER_TIMEOUT。
            per_attempt_timeout = (
                IMAGE_WORKER_TIMEOUT
                if "image_gen" in (nt.get("toolsets") or [])
                else WORKER_TIMEOUT
            )
            to = per_attempt_timeout * CHILD_MAX_ATTEMPTS
            try:
                out.append(f.result(timeout=to))
            except cf.TimeoutError:
                lbl = ticket.get("label") or "child"
                parent.log(f"子 agent {lbl} 超过 {to}s,放弃")
                with parent._spawn_lock:
                    if not ticket.get("recorded"):     # 边界:子可能恰好已记真实结果,别覆盖/重复
                        ticket["abandoned"] = True     # 让迟到的线程丢弃自己的记录
                        parent.worker_recs.append({"label": lbl, "clean": False, "renders": 0,
                                                   "shot": None, "exit_reason": "timeout"})
                message = f"子 agent 超过 {to}s 被放弃"
                out.append({"label": lbl, "status": "timeout", "renders": 0,
                            "shot": None, "summary": message,
                            "summary_path": None, "summary_chars": len(message),
                            "summary_truncated": False, "artifacts": []})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)   # 卡死的线程不阻塞 parent(已无法脏写共享状态)
    return json.dumps({"results": out}, ensure_ascii=False)


# ============= 入口 + 拒绝采样验收 =============

def _seed_to_brief(seed):
    """把一条 seed dict 变成给编排器的初始 brief。"""
    brief = seed["query"] if isinstance(seed, dict) else str(seed)
    if isinstance(seed, dict):
        extra = []
        if seed.get("slide_count"):
            extra.append(f"目标页数约 {seed['slide_count']} 页")
        if seed.get("lang"):
            extra.append(f"语言:{seed['lang']}")
        if extra:
            brief += "\n\n(" + ";".join(extra) + ")"
    return brief


MIN_RENDER_BYTES = int(os.environ.get("MIN_RENDER_BYTES", "26000"))   # 退路:观测到的纯色空白图 ~21KB
BLANK_LUMA_RANGE = int(os.environ.get("BLANK_LUMA_RANGE", "24"))      # 灰度跨度小于此 = 近乎纯色 = 空白/破渲染


def _render_ok(png):
    """判断一张渲染图不是空白/破图。优先用 PIL 看灰度跨度(纯色页跨度≈0,有任何文字/图形跨度都很大,
    不会误伤"稀疏但有效"的页);没装 PIL 时退回字节下限(>观测到的纯色空白 ~21KB)。"""
    try:
        from PIL import Image
        with Image.open(png) as im:
            lo, hi = im.convert("L").getextrema()
        return (hi - lo) >= BLANK_LUMA_RANGE
    except Exception:
        return os.path.getsize(png) >= MIN_RENDER_BYTES


def _accept(orch):
    """结构化验收 —— 只有干净轨迹才提交(拒绝采样)。返回 (ok, 原因)。"""
    if orch.exit_reason != "text_response":
        return False, f"编排器未自然收尾(exit={orch.exit_reason})"
    slides = sorted(glob.glob(os.path.join(orch.ws, "slides", "slide_*.html")))
    if not slides:
        return False, "没有产出任何 slide"
    # 每页都要有渲染图,且渲染图不能是空白/破图。
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
    with orch._spawn_lock:                      # 与 subagent 线程的写竞争,快照后再判
        recs = list(orch.worker_recs)
    bad = [
        w["label"]
        for w in recs
        if not w["clean"] and not w.get("superseded")
    ]
    if bad:
        return False, f"{len(bad)} 个 slide subagent 未通过: {bad[:5]}"
    return True, "ok"


def _link_skills(run_dir):
    """在工作区根放一个指向只读 skill 树的符号链接,让 bash(cwd=工作区)能用相对路径
    `skills/ppt-skill/scripts/render.py` 跑脚本——和 read('skills/...') 的路径一致。"""
    link = os.path.join(run_dir, "skills")
    if not os.path.lexists(link):
        try:
            default_ppt_skill = os.path.normpath(os.path.join(SKILLS_DIR, "ppt-skill"))
            if os.path.normpath(PPT_SKILL_DIR) == default_ppt_skill:
                os.symlink(SKILLS_DIR, link)
            else:
                os.makedirs(link, exist_ok=True)
                os.symlink(PPT_SKILL_DIR, os.path.join(link, "ppt-skill"))
        except Exception:
            pass


def run_sample(sample_id, seed, run_dir, config):
    """distill.py 的入口契约。跑编排器(它会并行委派 subagent),做结构化验收,返回状态 dict。"""
    _link_skills(run_dir)
    orch = Agent(role="orchestrator", sid=sample_id, ws=run_dir, sub_dir="orchestrator",
                 tools_schema=tools.orchestrator_tools(), config=config,
                 initial_user=build_initial_user_content(seed, _seed_to_brief(seed)), label="orch")
    orch.run()
    ok, reason = _accept(orch)
    slides = glob.glob(os.path.join(run_dir, "slides", "slide_*.html"))
    with orch._spawn_lock:
        workers = list(orch.worker_recs)
    return {
        "status": "completed" if ok else "rejected",
        "reason": reason,
        "n_slides": len(slides),
        "n_workers": len(workers),
        "orch_exit": orch.exit_reason,
        "workers": workers,
        "pid": os.getpid(),
    }
