#!/usr/bin/env python3
"""Long-Horizon Presenter 的 agent 运行时与委派适配层。

- `Agent`:单 agent 的状态 + 沙箱(safe/read_path/writable)+ 模型客户端 + 一个 Trace。
- `run_loop(agent)`:**纯** ReAct 循环(模型 → 工具 → 模型),模型不再调工具即收尾,或到 max_turns;不替模型兜底。
- `delegate_task(parent, …)`:并行起一批子 agent,各在独立上下文里干活,只把结构化交接返回父级。

子 agent 由 `goal`(干什么)+ `toolsets`(获得哪些能力)在调用时拼出；本专属 Harness
只补齐 Presenter 所需的最小运行契约，例如 Slide/Review 必须实际获得 Vision。

进程/线程模型:sample 之间 = 进程(presenter.py 调度);一个 sample 内的子 agent = 线程
(每次 delegate_task 起一个**本地** ThreadPoolExecutor,用完即关 → 不跨 sample 串台)。
"""
import base64
import concurrent.futures as cf
import copy
import glob
import hashlib
import json
import os
import random
import re
import threading
import time

import anthropic

from . import _final_contract, tools
from . import nova_bridge
from .trace import Trace

# 子 agent 并发上限(所有 delegate_task 调用共用一个父级信号量)。
MAX_CONCURRENT_CHILDREN = int(os.environ.get("MAX_CONCURRENT_CHILDREN",
                              os.environ.get("SLIDE_CONCURRENCY", "4")))
# 同回合内**纯 IO 叶子工具**(无 agent 状态记账、落盘内容寻址不撞名)并行执行——主要打掉 image subagent
# 里多张 image_generate 的串行;单独限并发 TURN_TOOL_PARALLEL 防出图/搜索网关过载。可用 env 覆盖。
PARALLEL_LEAF_TOOLS = set(x for x in os.environ.get("PARALLEL_LEAF_TOOLS", "image_generate,web_search,web_extract").split(",") if x)
TURN_TOOL_PARALLEL = int(os.environ.get("TURN_TOOL_PARALLEL", "6"))
WORKER_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "1500"))           # 普通子 agent 硬超时(秒)。900→1500(2026-07-17):高并发下 API 429 退避把每 turn 拉到~37s,v2.1 slide 需24-32turn→撞900s被杀→无summary→orch判未通过→整deck被拒(611次超时事故)。抬到1500给足墙钟;真正治竞争靠降WK
IMAGE_WORKER_TIMEOUT = int(os.environ.get("IMAGE_WORKER_TIMEOUT", "1800"))  # 含 image_gen 的子 agent 硬超时
MATERIAL_WORKER_TIMEOUT = int(os.environ.get("MATERIAL_WORKER_TIMEOUT", "1500"))  # material 子 agent:自己跑解析脚本(pdf/office解析+扫描PDF光栅化)吃时间,给更高预算(2026-07-09 解析交 agent)
# 2026-07-10 失败根因:timeout 240(review 127+slide 86 主导),review 逐页 vision_analyze、与页数强相关
# (review-timeout deck 中位 24 页 vs 全体 12);slide 是 patch↔render↔vision 自纠环。→ 按角色/页数弹性放大超时。
SLIDE_WORKER_TIMEOUT_BASE = int(os.environ.get("SLIDE_WORKER_TIMEOUT_BASE", "900"))
SLIDE_WORKER_TIMEOUT_PER_EXTRA_PAGE = int(os.environ.get("SLIDE_WORKER_TIMEOUT_PER_EXTRA_PAGE", "180"))
SLIDE_WORKER_TIMEOUT_CAP = int(os.environ.get("SLIDE_WORKER_TIMEOUT_CAP", "2400"))
REVIEW_WORKER_TIMEOUT_BASE = int(os.environ.get("REVIEW_WORKER_TIMEOUT_BASE", "900"))       # review 起步预算
REVIEW_WORKER_TIMEOUT_PER_PAGE = int(os.environ.get("REVIEW_WORKER_TIMEOUT_PER_PAGE", "45"))# 每页 +45s(逐页看图)
REVIEW_WORKER_TIMEOUT_CAP = int(os.environ.get("REVIEW_WORKER_TIMEOUT_CAP", "1800"))        # 封顶
MATERIAL_WORKER_TIMEOUT_PER_PAGE = int(os.environ.get("MATERIAL_WORKER_TIMEOUT_PER_PAGE", "30"))  # material 大 deck 每页 +30s
MATERIAL_WORKER_TIMEOUT_CAP = int(os.environ.get("MATERIAL_WORKER_TIMEOUT_CAP", "3600"))          # material 封顶(2026-07-15 2700→3600:超重附件集消化 >45min 会 timeout→clean=False 误杀;给足时间自然收尾)

# 活跃上下文只保留近期工具细节。完整原始消息仍在 Trace 中，这里只压缩
# 已经被后续回合消费的早期文本/工具结果，避免长 Research、Material、Review 越跑越慢。
HISTORY_COMPACT_AFTER_CHARS = int(os.environ.get("HISTORY_COMPACT_AFTER_CHARS", "120000"))
HISTORY_KEEP_RECENT_MESSAGES = int(os.environ.get("HISTORY_KEEP_RECENT_MESSAGES", "10"))
HISTORY_TOOL_RESULT_MAX_CHARS = int(os.environ.get("HISTORY_TOOL_RESULT_MAX_CHARS", "1400"))
HISTORY_TEXT_MAX_CHARS = int(os.environ.get("HISTORY_TEXT_MAX_CHARS", "1000"))


def _deck_n_slides(parent):
    """粗数当前工作区已产出的页 HTML(给 review/material 超时按页数缩放用;数不到返 0=退回基础预算)。"""
    try:
        import glob as _g
        ws = getattr(parent, "ws", "") or ""
        return len(_g.glob(os.path.join(ws, "slides", "slide_*.html")))
    except Exception:
        return 0
SUBAGENT_MAX_TOKENS = int(os.environ.get("SUBAGENT_MAX_TOKENS", "16000"))  # 叶子子 agent per-回合上限(orch 才需大值;子 agent 大值只拖慢踩超时)
# Slide Group 会一次制作多页。单页时仍保留原有宽松止损线；每增加一页，
# 增加读计划、实现和像素复验预算。这是异常 backstop，不是要求 Agent 用完配额。
SLIDE_MAX_TURNS_BASE = int(os.environ.get("SLIDE_MAX_TURNS_BASE", "36"))
SLIDE_MAX_TURNS_PER_EXTRA_PAGE = int(os.environ.get("SLIDE_MAX_TURNS_PER_EXTRA_PAGE", "12"))
SLIDE_MAX_TURNS_CAP = int(os.environ.get("SLIDE_MAX_TURNS_CAP", "120"))
MAX_REVIEW_ATTEMPTS = int(os.environ.get("MAX_REVIEW_ATTEMPTS", "3"))
MAX_SLIDE_REPAIR_ATTEMPTS = int(os.environ.get("MAX_SLIDE_REPAIR_ATTEMPTS", "2"))
# Research is a task-level singleton for its SUCCESS state (one active ready
# Research), but a first timeout/crash/blocked must not permanently exhaust the
# slot — allow a bounded controlled recovery.
MAX_RESEARCH_ATTEMPTS = int(os.environ.get("MAX_RESEARCH_ATTEMPTS", "2"))
MAX_SPAWN_DEPTH = int(os.environ.get("MAX_SPAWN_DEPTH", "1"))           # 委派深度上限(1 = 只有顶层能委派)
# Vision 不设“单 Agent 累计看图数”上限。一张图在紧随的模型回合被看到后，
# 便从**活跃模型上下文**里释放；真实像素仍由 Trace 快照永久保留。因此 36/80
# 页 Review 可以分批连续看完，无需因历史图片累积而 blocked，也无需重派 Review。
RESAMPLE_THINKING_ONLY = int(os.environ.get("RESAMPLE_THINKING_ONLY", "2"))  # >0: 传输层重采 thinking-only 退化空采样(丢弃不入轨迹);0=纯 loop 原行为。2026-07-10 默认 0→2:stopped_no_text 失败 159 中 92% 是 thinking-only/空退化,重采可吃掉主体
CONTRACT_CLOSEOUT_RETRIES = int(os.environ.get("CONTRACT_CLOSEOUT_RETRIES", "1"))
# 每个 tool_result 后追加一条引导思考的 text 块(=在工具结果 user 回合尾部拼一句),把一次性 system nudge
# 升级成"每轮工具后强制提醒",提高交错思考(reasoning summary + signature)触发率。默认关。
# ⚠️ 该 text 会进 messages/轨迹 → 若不想让 nudge 落进训练数据,pack 时按 THINK_NUDGE_TEXT 首句剥离(见 trace_to_openai)。
THINK_NUDGE_EACH_TOOL = os.environ.get("THINK_NUDGE_EACH_TOOL", "0") == "1"
THINK_NUDGE_TEXT = os.environ.get("THINK_NUDGE_TEXT",
    "After receiving the tool result(s) above, carefully reflect on their quality and "
    "determine optimal next steps before proceeding. Use your thinking to plan and iterate "
    "based on this new information, and then take the best next action.")
# 限流/过载(429/529/503/500/502)专用重试:网关并发突发时 review 子 agent 易吃 429。
# 这类是**瞬时可恢复**的传输层错误,默认 4 次 ~30s 退避在持续突发下不够 → 拉长。
# 指数退避 + 抖动,封顶 RETRY_BACKOFF_CAP 秒;尊重响应里的 Retry-After。非替模型兜底,纯传输韧性。
API_MAX_RETRIES = int(os.environ.get("API_MAX_RETRIES", "8"))       # 限流类最多重试次数
RETRY_BACKOFF_CAP = int(os.environ.get("RETRY_BACKOFF_CAP", "45"))  # 单次退避上限(秒)
# 触发"加长退避"的瞬时状态码(其余非 400 异常仍走原 4 次短退避)
_TRANSIENT_STATUS = {429, 500, 502, 503, 529}


def _runtime_time_context(started_epoch, language):
    """Return one authoritative timestamp shared by the whole agent tree."""
    started_utc = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(started_epoch))
    )
    if language == "zh":
        return (
            f"权威时间上下文：本任务开始于 {started_utc}。判断已发生、当前、未来和"
            "资料日期时以此为准，不使用模型记忆中的当前日期。"
        )
    return (
        f"Authoritative time context: this task started at {started_utc}. "
        "Use this timestamp for past/current/future and source-date judgments; "
        "do not rely on a remembered current date."
    )


def _infer_prompt_language(text):
    """Infer the primary language of the current user request."""
    value = str(text or "")
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", value))
    latin_chars = len(re.findall(r"[A-Za-z]", value))
    if not cjk_chars:
        return "en"
    if not latin_chars:
        return "zh"
    # CJK encodes a word in far fewer characters than Latin script; weight it so
    # common English technical terms do not flip an otherwise Chinese request.
    return "zh" if cjk_chars * 4 >= latin_chars else "en"


def _visible_response_language_context(language):
    if language == "zh":
        return (
            "可见回复语言：过程说明、可见的 reasoning/thinking、工具调用前说明和最终总结使用中文；"
            "代码、路径、原文与专有名词可保留原文。"
            "PPT 屏显和讲稿语言仍服从用户的交付要求。"
        )
    return (
        "Visible response language: use English for progress notes, any visible "
        "reasoning/thinking, tool-call "
        "preambles, and final summaries. Code, paths, quotations, and proper nouns "
        "may remain in their original language. Deck and speech language still "
        "follows the user's delivery requirement."
    )


def _child_language_contract(language):
    """Make the parent's response language explicit in every delegated task.

    The system prompt remains the authority, but weaker models often follow the
    task body more reliably than a distant system paragraph during long tool
    loops.  Delivery language stays plan-owned because it may intentionally
    differ from the raw query language.
    """
    if language == "zh":
        return (
            "Response language: 中文。所有可见 reasoning/thinking、进度说明、"
            "工具调用前说明和最终总结必须使用中文；代码、路径、原文和专有名词可保留原文。\n"
            "Deliverable language: 严格遵循 plan/deck.md 与用户要求；"
            "不得从角色卡语言或模型默认语言推断。\n\n"
        )
    return (
        "Response language: English. Use English for all visible reasoning/thinking, "
        "progress notes, tool-call preambles, and final summaries; code, paths, "
        "quotations, and proper nouns may remain in their original language.\n"
        "Deliverable language: follow plan/deck.md and the user's requirement; "
        "never infer it from the role-card language or model default.\n\n"
    )


def blocks_to_dicts(content):
    """把 anthropic SDK 的 content blocks 转成可序列化 dict(供 messages 列表与轨迹复用)。"""
    out = []
    for b in content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif b.type == "thinking":
            sig = getattr(b, "signature", None)
            # 无签名的 thinking 块(签名被上游代理剥离,或 max_tokens 在 thinking 中途截断未生成签名)
            # 一旦发回 API 必触发 400 "signature: Field required" → 不重试 → api_failed → 整条 deck 被拒。
            # 这种块本就不可回传(API 强制要签名),丢弃它严格不劣于现状:常规有签名块完全不变,
            # 同一 assistant 轮里的 tool_use/text 仍保留(不会产生空 content);只有被拒轨迹会少一段截断思考。
            if not sig:
                continue
            out.append({"type": "thinking", "thinking": b.thinking, "signature": sig})
        elif b.type == "redacted_thinking":
            out.append({"type": "redacted_thinking", "data": b.data})
    return out


def _canonical_agent_label(role, label):
    """Canonical public identity for logs; keep trace directory names separate."""
    value = str(label or "").strip()
    role_name = str(role or "").strip().lower()
    if role_name == "orchestrator" or value.lower() in {"orch", "orchestrator"}:
        return "orch"
    return value


class Agent:
    """通用单 Agent 循环(模型 + 工具);Agent 对象 = 状态,循环逻辑在下面的 run_loop。

    编排器和子 agent 共用这个类,区别只在:工具集、初始任务、system、只读写前缀,以及编排器
    额外注册了 delegate_task。父子**共享同一个工作区 ws**(协作产出同一套产物),但各写各的轨迹。"""

    def __init__(self, role, sid, ws, sub_dir, tools_schema, config, initial_user, label,
                 system, skills_root, forbid_write_prefixes=None, extra_tools=None):
        self.role = role
        self.sid = sid
        self.ws = os.path.abspath(ws)                 # 工作区 = run_dir
        self.skills_root = os.path.normpath(os.path.abspath(skills_root)) if skills_root else None
        self.forbid_write_prefixes = list(forbid_write_prefixes or [])
        self.initial_user = initial_user
        # ``orchestrator`` is the durable role/trace-directory name, while the
        # public event stream uses one canonical identity: ``orch``.  Normalize
        # here so fresh logs, config snapshots and revision runs never re-create
        # the historical two-name split.
        self.label = _canonical_agent_label(role, label)
        self.extra_tools = extra_tools or {}
        self.tools = tools_schema
        self.cfg = config or {}
        self.protected_runtime_paths = list(self.cfg.get("_protected_runtime_paths") or [])
        self.started = time.time()
        self.task_started_epoch = float(
            self.cfg.get("_task_started_epoch", self.started)
        )
        prompt_language = str(self.cfg.get("_prompt_language") or "").lower()
        if prompt_language not in {"zh", "en"}:
            prompt_language = _infer_prompt_language(initial_user)
        self.prompt_language = prompt_language
        self.generation_preferences = dict(self.cfg.get("_generation_preferences") or {})
        self.base_system = system.rstrip()
        self.system = (
            f"{self.base_system}\n\n"
            f"{_runtime_time_context(self.task_started_epoch, prompt_language)}\n"
            f"{_visible_response_language_context(prompt_language)}\n"
        )
        if self.generation_preferences:
            settings = json.dumps(self.generation_preferences, ensure_ascii=False, sort_keys=True)
            self.system += (
                "\nRuntime presentation settings selected in the product UI are authoritative. "
                "Apply them in planning, page production, review, and delivery; do not reinterpret "
                f"or silently drop them: {settings}\n"
            )
            if self.role == "orchestrator" and int(
                self.generation_preferences.get("attachment_count") or 0
            ):
                self.system += (
                    "\nThis run contains staged user attachments. Follow the selected Skill's "
                    "Material stage exactly: delegate the staged attachment paths to Material "
                    "sub-agents and wait for their ready/complete coverage contracts before using "
                    "the material in planning. Do not replace that stage by reading and summarizing "
                    "the attachments yourself.\n"
                )
        trace_root = os.path.join(self.ws, "_trace")
        trace_namespace = str(self.cfg.get("_trace_namespace") or "").strip("/")
        if trace_namespace:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", trace_namespace):
                raise ValueError(f"非法 trace namespace: {trace_namespace!r}")
            trace_root = os.path.join(trace_root, trace_namespace)
        self.trace = Trace(os.path.join(trace_root, sub_dir))

        # —— tools.py 依赖的上下文字段 ——
        self.serper = os.environ.get("SERPER_API_KEY")
        self.image_provider = str(
            self.cfg.get("image_provider")
            or os.environ.get("IMAGE_PROVIDER")
            or "openai_images"
        ).strip().lower()
        self.img_base = str(
            self.cfg.get("image_base_url")
            or os.environ.get("IMAGE_BASE_URL")
            or self.cfg.get("openai_base_url")
            or os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")
        ).rstrip("/")
        self.img_key = os.environ.get("IMAGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.image_model = self.cfg.get("image_model", os.environ.get("IMAGE_MODEL", "gpt-image-2-pro-all"))
        self.img_n = 0

        # —— 运行状态 ——
        self.last_shot = None       # 最近一次成功 render 的工作区相对路径
        self.n_renders = 0
        self._usage_acc = {"sum_input": 0, "sum_cache_read": 0, "sum_cache_create": 0, "sum_output": 0, "n_turns": 0}  # ① cache 记账
        self.n_vision_imgs = 0      # 本 agent 累计成功看过的直接图片（仅指标，不是配额）
        self.n_vision_calls = 0     # 成功取得像素/视觉分析的调用数；B 视觉后端同样计数
        self.vision_paths = []      # 成功看过的工作区图片路径；用于核验 Image 派生资产
        self.vision_evidence = {}   # path -> 当时像素的 sha256 / mtime；每次成功调用独立落盘
        self.final_text = ""
        self.exit_reason = None
        self.worker_recs = []       # 仅编排器:每个子 agent 的小结
        self._spawn_count = {}
        self._role_spawn_count = {}  # Research 单例；Review 允许有限复验
        self._slide_page_owners = {}  # page -> 唯一 Production Group label
        self._spawn_lock = threading.Lock()
        self._child_sem = threading.Semaphore(MAX_CONCURRENT_CHILDREN)   # 父级并发闸
        self._delegate_depth = 0
        # —— 模型客户端 ——
        self.model = self.cfg.get(
            "model", os.environ.get("MODEL", os.environ.get("SENSENOVA_MODEL_NAME", "deployment-model"))
        )
        self.a_base = self.cfg.get("anthropic_base_url",
                                   os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"))
        self.max_turns = int(self.cfg.get("max_turns", 120))
        self.max_tokens = int(self.cfg.get("max_tokens", 16000))
        self.requested_thinking = os.environ.get(
            "STUDIO_REQUESTED_THINKING", os.environ.get("THINKING", "0")
        ) != "0"
        self.effective_thinking = os.environ.get(
            "STUDIO_EFFECTIVE_THINKING", os.environ.get("THINKING", "0")
        ) != "0"
        self.thinking_transport = os.environ.get("STUDIO_THINKING_TRANSPORT", "")
        self.thinking = self.effective_thinking
        self.think_effort = os.environ.get("THINK_EFFORT", "high")
        # 网关上带 `-thinking` 后缀的模型思考是**内置**的:纯调用即自带 thinking 块,反而传
        # anthropic 的 thinking/output_config 参数会把思考压掉。故对这类模型一律走纯调用
        # (thinking 块照样回来并被 blocks_to_dicts 记进轨迹)。
        self.native_thinking = "thinking" in self.model.lower()
        if self.native_thinking:
            self.thinking = False
            self.effective_thinking = True
        # beta 头与 cache_control 解耦(2026-06-30):
        #   - 官方现代法 = 只在 system 块挂 cache_control,**不需 legacy beta 头**(SDK/上游已 GA)。
        #   - A key(sk-bpAc)上游=AWS Bedrock 还会**拒** beta 头(400 invalid beta flag)。
        #   ⟹ beta 头默认**不发**(CACHE_BETA_HEADER=1 才发,只为个别老上游);cache_control 仍由 PROMPT_CACHE 控(默认开)。
        _default_headers = ({"anthropic-beta": "prompt-caching-2024-07-31"}
                            if os.environ.get("CACHE_BETA_HEADER", "0") != "0" else {})
        if os.environ.get("MODEL_BACKEND", "").lower() == "openai":
            # 学生模型(vLLM,OpenAI 兼容):鸭子化 anthropic 客户端,让 v1.2 引擎原样驱动 9B/27B。
            # 纯加法,只在 MODEL_BACKEND=openai 时生效;Anthropic 走下面 else,行为不变。
            from . import openai_backend
            self.model = self.cfg.get("model") or os.environ.get("STUDENT_MODEL", self.model)
            self.thinking = False   # OpenAI 兼容模型由请求体 transport 控制，不传 Anthropic 参数
            self.client = openai_backend.OpenAIShim(
                base=os.environ["STUDENT_BASE_URL"],
                model=self.model,
                key=os.environ.get("STUDENT_API_KEY", "EMPTY"))
        else:
            self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=self.a_base,
                                              default_headers=_default_headers)

        self.nova_raw = nova_bridge.create_recorder(self)
        self.nova_precheck = None

        if any(t.get("name") == "delegate_task" for t in self.tools):
            self.extra_tools.setdefault("delegate_task", lambda **a: delegate_task(self, **a))

    def log(self, m):
        now = time.time()
        elapsed = max(0.0, now - self.task_started_epoch)
        print(
            f"[{time.strftime('%H:%M:%S', time.localtime(now))} +"
            f"{elapsed:6.1f}s] [{self.sid}/{self.label}] {m}",
            flush=True,
        )

    # —— 沙箱:写/渲染限定在 ws 内;读还可读只读的 skills_root 树 ——
    def safe(self, path):
        p = os.path.normpath(os.path.join(self.ws, path))
        if p != os.path.normpath(self.ws) and not p.startswith(os.path.normpath(self.ws) + os.sep):
            raise ValueError(f"路径越出工作区: {path}")
        return p

    def writable(self, path):
        """写入策略:命中 forbid_write_prefixes 的路径只读(如编排器不许写 slides/)。"""
        p = os.path.normpath(os.path.join(self.ws, path))
        for pre in self.forbid_write_prefixes + self.protected_runtime_paths:
            d = os.path.normpath(os.path.join(self.ws, pre))
            if p == d or p.startswith(d + os.sep):
                return False
        return True

    def read_path(self, path):
        """可读路径:ws 内,或只读的 skills_root 树(路径以 'skills' 开头时映射过去)。"""
        if self.skills_root and (path == "skills" or path.startswith("skills/")):
            rel = path[len("skills"):].lstrip("/")
            p = os.path.normpath(os.path.join(self.skills_root, rel))
            if p != self.skills_root and not p.startswith(self.skills_root + os.sep):  # 防同前缀越界
                raise ValueError("路径越出 skills")
            return p
        return self.safe(path)

    def config_snapshot(self):
        tool_names = [str(tool.get("name") or "") for tool in self.tools]
        return {
            "role": self.role, "sample_id": self.sid, "label": self.label,
            "task": self.initial_user, "model": self.model, "anthropic_base_url": self.a_base,
            "image_provider": self.image_provider, "image_model": self.image_model,
            "max_tokens": self.max_tokens, "max_turns": self.max_turns,
            "serper": bool(self.serper), "pid": os.getpid(),
            "thinking": (
                {"type": self.thinking_transport, "enabled": self.effective_thinking}
                if self.thinking_transport else
                ("native" if self.native_thinking else
                 ({"type": "adaptive", "display": "summarized", "effort": self.think_effort} if self.effective_thinking else False))
            ),
            "requested_thinking": self.requested_thinking,
            "effective_thinking": self.effective_thinking,
            "thinking_transport": self.thinking_transport,
            "generation_preferences": self.generation_preferences,
            "authoritative_task_started_at": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.task_started_epoch)
            ),
            # Persist an absolute instant.  The old value used the host's local
            # wall clock without an offset, so consumers on macOS/UTC hosts
            # could interpret the same run eight hours apart and clamp a real
            # Agent duration to zero.
            "started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started)
            ),
            "tools": tool_names,
            "vision_available": "vision_analyze" in tool_names,
        }

    def run(self):
        try:
            return run_loop(self)
        except Exception as exc:
            nova_bridge.abort_agent(self, exc)
            raise


# ===================== 循环(自由函数,操作一个 agent) =====================

def _anthropic_tool(t):
    """把 hermes 风格的 {name, description, parameters} 适配成 Anthropic Messages API 要的
    {name, description, input_schema}。schema 内容(name/描述/参数)与 hermes 逐字一致,只换外层键。"""
    return {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}


# prompt caching 开关(默认开;PROMPT_CACHE=0 关)。把 cache_control 挂在**两个位置**:
#   ① system 改成 list-of-blocks、在 block 上挂 cache_control —— 缓存 [tools + system] 这段稳定前缀
#      (Anthropic 缓存前缀顺序为 tools→system→messages,故 system 断点也覆盖其前的 tools)。
#   ② 顶层 cache_control —— 走 SDK 的 extra_body 透传进请求体(SDK 无此具名参数,直接传 kwarg 会 TypeError)。
# ⚠️ tokenhub cache 命中随渠道路由在变(0622 全 0 → 2026-07-03 平台B实测已命中且无需 beta 头:
#    全新前缀 create=14062→read=14062)。故 cache_control 默认发(PROMPT_CACHE=1),落到缓存渠道即省;
#    命中率看真 run 的 usage.json cache_read(单次会飘,别当永久)。beta 头默认不发(见下,Bedrock 上游会 400 拒)。
PROMPT_CACHE = os.environ.get("PROMPT_CACHE", "1") != "0"
_EPHEMERAL = {"type": "ephemeral"}

# 多轮对话历史缓存(2026-07-09,三方A/Bedrock 实测 +40pt):在**发送用副本**的最后一条 message 的
# 最后一个 content block 上再挂一个 cache_control 断点,把 "system + 到当前为止的全部历史" 变成可复用前缀。
# 下一轮请求的前缀 = 上一轮缓存过的内容 → 命中(隔离实验 sys-only 47.5% → sys+last 87.7%)。
#   · 只加断点、不改 agent 持有的 messages(不污染下轮/落盘);str content 先转成 text block。
#   · system 断点保留 → 共 2 个断点(Bedrock 上限 4)。CACHE_MESSAGES=0 可关(回退旧行为)。
#   · Bedrock 只拒**顶层** cache_control,不拒 message block 级(已实测 create→read 稳定命中)。
CACHE_MESSAGES = os.environ.get("CACHE_MESSAGES", "1") != "0"


def _msgs_with_cache_bp(messages):
    """返回 messages 的浅层安全副本,在最后一条 message 的最后一个 content block 挂 cache_control。
    只深拷最后一条(其余共享引用,省开销);content 为 str 时转成 [{"type":"text",...}]。失败则原样返回。"""
    if not messages:
        return messages
    try:
        out = list(messages)                       # 浅拷列表
        last = copy.deepcopy(out[-1])              # 只深拷最后一条,避免改到 agent 持有对象
        c = last.get("content")
        if isinstance(c, str):
            last["content"] = [{"type": "text", "text": c, "cache_control": _EPHEMERAL}]
        elif isinstance(c, list) and c:
            # 找最后一个 dict block(tool_result/text/...)挂断点;跳过非 dict
            for blk in reversed(c):
                if isinstance(blk, dict):
                    blk["cache_control"] = _EPHEMERAL
                    break
        else:
            return messages                        # 空/异常 content,不动
        out[-1] = last
        return out
    except Exception:
        return messages



def _acc_usage(agent, resp):
    """累加一次模型调用的 usage 到 agent._usage_acc(cache_* 可能为 None,(x or 0) 兜底)。非破坏:只读 resp.usage。"""
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        acc = agent._usage_acc
        acc["sum_input"]        += getattr(u, "input_tokens", 0) or 0
        acc["sum_cache_read"]   += getattr(u, "cache_read_input_tokens", 0) or 0
        acc["sum_cache_create"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        acc["sum_output"]       += getattr(u, "output_tokens", 0) or 0
        acc["n_turns"]          += 1
    except Exception:
        pass


def _write_usage(agent):
    """收尾把 usage 聚合写到 _trace/<role>/usage.json。容错:失败不影响轨迹。"""
    try:
        acc = agent._usage_acc
        # 命中率 = 读缓存 / 总输入。总输入 = 新输入 + 读缓存 + **写缓存**(cache_create 也是被处理的输入 token,
        # 之前漏掉它 → 分母偏小 → 命中率被严重高估,如 53776/(53776+12)=99.98% 实则 53776/95500=56.3%)。
        denom = acc["sum_cache_read"] + acc["sum_input"] + acc["sum_cache_create"]
        out = dict(acc)
        out["hit_rate"] = (acc["sum_cache_read"] / denom) if denom else None
        with open(os.path.join(agent.trace.sub_dir, "usage.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        agent.log(f"[usage.json 写失败,忽略] {str(e)[:120]}")


def _model_call(agent, messages):
    """一次模型调用,带有限的传输层重试(B 类:网络抖动,非替模型兜底)。工具恒挂。"""
    system = agent.system
    extra_body = None
    if PROMPT_CACHE and system:
        system = [{"type": "text", "text": agent.system, "cache_control": _EPHEMERAL}]  # 位置②:system block
        extra_body = {}
        if os.environ.get("CACHE_TOPLEVEL", "1") != "0":
            extra_body["cache_control"] = _EPHEMERAL
        if os.environ.get("CACHE_USER_ID", ""):
            extra_body["metadata"] = {"user_id": os.environ["CACHE_USER_ID"]}
        extra_body = extra_body or None                                       # 位置①:顶层
    # 位置③:messages 历史断点(多轮命中率优化,见 _msgs_with_cache_bp)
    send_messages = _msgs_with_cache_bp(messages) if (PROMPT_CACHE and CACHE_MESSAGES) else messages
    kwargs = dict(model=agent.model, max_tokens=agent.max_tokens, system=system, messages=send_messages,
                  tools=[_anthropic_tool(t) for t in agent.tools])
    if extra_body:
        kwargs["extra_body"] = extra_body
    if agent.thinking:
        # Anthropic 接口:effort 属于 output_config;display 必须显式 "summarized" 才能把(摘要版)推理写进轨迹。
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        kwargs["output_config"] = {"effort": agent.think_effort}
    if agent.nova_raw is not None:
        response = nova_bridge.call_main(agent, kwargs)
        if response is not None:
            _acc_usage(agent, response)
        return response
    attempt = 0
    while True:
        try:
            # 大 max_tokens 非流式触发 SDK >10min 守卫;流式规避,get_final_message 同型返回
            with agent.client.messages.stream(**kwargs) as _stream:
                _resp = _stream.get_final_message()
            _acc_usage(agent, _resp)   # ① 累加本回合 usage
            return _resp
        except anthropic.BadRequestError as e:
            agent.log(f"[api 400 不重试] {str(e)[:300]}")   # 400 是请求本身的问题,重试不会变好
            return None
        except Exception as e:
            status = _status_code(e)
            transient = status in _TRANSIENT_STATUS
            # 限流/过载:用更宽的次数 + 指数退避(尊重 Retry-After);其余抖动错误保持原 4 次短退避。
            cap = API_MAX_RETRIES if transient else 4
            if attempt >= cap - 1:
                tag = f"{status} 限流/过载" if transient else "传输错误"
                agent.log(f"[api err {attempt} 放弃·{tag}] {str(e)[:160]}")
                return None
            if transient:
                ra = _retry_after(e)
                if ra is not None:
                    wait = ra                                    # 服务器明确要求等多久就等多久(优先)
                elif attempt == 0:
                    wait = 0                                     # 第一次马上重试(瞬时抖动,可能是网络一闪)
                else:
                    wait = min(RETRY_BACKOFF_CAP, 2 ** attempt)  # 第二次起指数退避(4,8,16…封顶45s)
                wait += random.uniform(0, 1.0)                   # 抖动:错开多 worker 同时重试,避免再次撞墙
                agent.log(f"[api err {attempt}·{status} 退避 {wait:.1f}s] {str(e)[:140]}")
            else:
                wait = (0 if attempt == 0 else 3 * attempt) + random.uniform(0, 0.8)  # 传输错误:首次马上,其后 3,6,9s
                agent.log(f"[api err {attempt} 退避 {wait:.1f}s] {str(e)[:160]}")
            time.sleep(wait)
            attempt += 1


def _status_code(e):
    """从 anthropic SDK 异常里取 HTTP 状态码(取不到返回 None)。"""
    for attr in ("status_code",):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "response", None)
    sc = getattr(resp, "status_code", None)
    return sc if isinstance(sc, int) else None


def _retry_after(e):
    """读响应 Retry-After 头(秒)。取不到/非法返回 None。封顶 RETRY_BACKOFF_CAP。"""
    resp = getattr(e, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        v = headers.get("retry-after") or headers.get("Retry-After")
        if v is None:
            return None
        return min(RETRY_BACKOFF_CAP, max(0.0, float(v)))
    except Exception:
        return None


def _vision_evidence_path(agent):
    return os.path.join(agent.trace.sub_dir, "vision-evidence.json")


def _persist_vision_evidence(agent):
    """Atomically persist successful vision evidence independently of handoff.

    Review completion text and pixel evidence have different lifecycles.  A
    weak model may finish inspecting pixels and then stall before emitting its
    final contract; writing this sidecar after every successful vision call
    prevents that protocol failure from erasing already observed pixels.
    """
    paths = list(dict.fromkeys(
        str(path).replace("\\", "/")
        for path in getattr(agent, "vision_paths", [])
        if str(path or "").strip()
    ))
    evidence = getattr(agent, "vision_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
    payload = {
        "version": 1,
        "vision_calls": int(getattr(agent, "n_vision_calls", 0) or 0),
        "vision_paths": paths,
        "vision_evidence": evidence,
    }
    target = _vision_evidence_path(agent)
    temporary = target + f".{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        agent.log(f"WARN: 视觉证据独立落盘失败: {exc}")


def _restore_vision_evidence(agent):
    """Restore vision metadata from the sidecar or immutable Trace snapshots.

    The snapshot fallback is deliberately byte-exact: a current workspace
    image is recovered only when its SHA256 matches a view_NN.png captured by
    Trace.  A file modified after inspection therefore remains stale instead
    of being accidentally certified.
    """
    restored_paths = []
    restored_evidence = {}
    restored_calls = 0
    sidecar = _vision_evidence_path(agent)
    try:
        with open(sidecar, encoding="utf-8") as stream:
            payload = json.load(stream)
        if isinstance(payload, dict):
            restored_paths.extend(payload.get("vision_paths") or [])
            if isinstance(payload.get("vision_evidence"), dict):
                restored_evidence.update(payload["vision_evidence"])
            restored_calls = int(payload.get("vision_calls") or 0)
    except (OSError, ValueError, TypeError):
        pass

    # Backward-compatible recovery for traces written before the sidecar was
    # introduced.  tool_log supplies candidate paths; immutable view snapshots
    # prove exactly which current bytes were really inspected.
    tool_log_path = os.path.join(agent.trace.sub_dir, "tool_log.json")
    snapshot_hashes = set()
    for snapshot in glob.glob(os.path.join(agent.trace.sub_dir, "images", "view_*.png")):
        try:
            with open(snapshot, "rb") as stream:
                snapshot_hashes.add(hashlib.sha256(stream.read()).hexdigest())
        except OSError:
            continue
    if snapshot_hashes:
        try:
            with open(tool_log_path, encoding="utf-8") as stream:
                tool_log = json.load(stream)
        except (OSError, ValueError, TypeError):
            tool_log = []
        candidates = []
        for item in tool_log if isinstance(tool_log, list) else []:
            if not isinstance(item, dict) or item.get("name") != "vision_analyze":
                continue
            args = item.get("args") or {}
            if isinstance(args, dict) and str(args.get("image_url") or "").strip():
                candidates.append(str(args["image_url"]).replace("\\", "/"))
        for path in dict.fromkeys(candidates):
            try:
                fp = agent.read_path(path)
                with open(fp, "rb") as stream:
                    digest = hashlib.sha256(stream.read()).hexdigest()
                mtime_ns = os.stat(fp).st_mtime_ns
            except OSError:
                continue
            if digest not in snapshot_hashes:
                continue
            restored_paths.append(path)
            restored_evidence[path] = {"sha256": digest, "mtime_ns": mtime_ns}

    current_paths = list(getattr(agent, "vision_paths", []) or [])
    current_evidence = getattr(agent, "vision_evidence", {})
    if not isinstance(current_evidence, dict):
        current_evidence = {}
    agent.vision_paths = list(dict.fromkeys(current_paths + restored_paths))
    agent.vision_evidence = {**restored_evidence, **current_evidence}
    agent.n_vision_calls = max(
        int(getattr(agent, "n_vision_calls", 0) or 0),
        restored_calls,
        len(agent.vision_evidence),
    )
    if agent.vision_paths or agent.vision_evidence:
        _persist_vision_evidence(agent)
    return {
        "vision_calls": agent.n_vision_calls,
        "vision_paths": list(agent.vision_paths),
        "vision_evidence": dict(agent.vision_evidence),
    }


def _exec_one_tool(agent, tu):
    """执行单个 tool_use,返回它的 tool_result(含 render 记账 / 图像快照 / is_error)。
    render 记账与图像快照会改 agent 状态,所以这些工具只在 _run_tools 里**顺序**调;
    delegate_task 不碰这些 parent 状态(子 agent 各写各的),可安全并行。"""
    args = dict(tu.input) if isinstance(tu.input, dict) else {}
    if tu.name == "vision_analyze":
        args["_parent_tool_use_id"] = str(tu.id or "")
    err = False
    try:
        res = tools.dispatch(agent, tu.name, args)
    except Exception as e:
        res = f"{tu.name} 崩溃: {e}"
        err = True
    if tu.name == "vision_analyze":
        # 只有真正得到图像或视觉后端分析才计数；路径不存在、格式错误和能力不可用不算。
        vision_ok = (
            isinstance(res, dict)
            and bool(res.get("image_b64") or res.get("vision_analysis"))
        )
        if vision_ok:
            agent.n_vision_calls += 1
            source_path = str(args.get("image_url") or "").strip()
            if source_path:
                agent.vision_paths.append(source_path)
                try:
                    fp = agent.read_path(source_path)
                    with open(fp, "rb") as stream:
                        digest = hashlib.sha256(stream.read()).hexdigest()
                    evidence = getattr(agent, "vision_evidence", None)
                    if not isinstance(evidence, dict):
                        evidence = {}
                        agent.vision_evidence = evidence
                    evidence[source_path.replace("\\", "/")] = {
                        "sha256": digest,
                        "mtime_ns": os.stat(fp).st_mtime_ns,
                    }
                except OSError:
                    pass
            _persist_vision_evidence(agent)
    # 通用 render 记账:terminal 命令 stdout 里的 .png 路径 → 当作渲染产出记账。
    # render.py 在 png 路径后还会打 ✓ RENDER_OK / ⚠ 版式警告,末行未必是 png,
    # 故从后往前找最后一个以 .png 结尾的行(兼容"路径后有诊断输出";取末行会漏记每次成功渲染)。
    if tu.name == "terminal" and isinstance(res, str) and not res.startswith("terminal 错误"):
        line = next((l.strip() for l in reversed(res.strip().splitlines())
                     if l.strip().endswith(".png")), "") if res.strip() else ""
        if line:
            agent.n_renders += 1
            try:
                agent.last_shot = os.path.relpath(line, agent.ws) if os.path.isabs(line) else line
            except Exception:
                agent.last_shot = line
    if isinstance(res, dict) and "image_b64" in res:
        tcid = tu.id
        # 快照照常存(轨迹完整,不受上限影响)
        try:
            agent.trace.snapshot_image(tcid, base64.b64decode(res["image_b64"]))
        except Exception as e:
            agent.log(f"WARN: 快照图像失败 {e}")
        agent.n_vision_imgs += 1
        return {"type": "tool_result", "tool_use_id": tcid, "content": [
            {"type": "text", "text": res.get("summary", "")},
            {"type": "image", "source": {"type": "base64", "media_type": res.get("media_type", "image/png"),
                                         "data": res["image_b64"]}}]}
    if isinstance(res, dict) and "vision_analysis" in res:
        return {
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": str(res.get("vision_analysis") or "")[:16000],
        }
    # delegate_task 已经把子轨迹压成结构化交接；不能再在工具适配层静默截断，
    # 否则父级会因缺字段转而读取整个 messages.json。其它叶子工具仍保持结果上限。
    content = str(res) if tu.name == "delegate_task" else str(res)[:8000]
    tr = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
    if err:
        tr["is_error"] = True     # 工具抛错 → 标 is_error,导出时据此把该 tool 消息标失败
    return tr


def _release_consumed_vision_images(messages):
    """释放已被模型看过的旧图，避免长 Deck 把历史 base64 一直带入后续请求。

    调用时机是“模型已对当前 messages 产生响应之后”，因此不会让本轮看图
    失效。Trace 在工具执行时已保存原始像素；模型若需要复看，可对该路径再次
    调用 vision_analyze。返回释放的图片数。
    """
    released = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            result_content = block.get("content")
            if not isinstance(result_content, list):
                continue
            next_content = []
            changed = False
            for item in result_content:
                if isinstance(item, dict) and item.get("type") == "image":
                    released += 1
                    changed = True
                    continue
                next_content.append(item)
            if changed:
                next_content.append({
                    "type": "text",
                    "text": (
                        "[该图像已在上一模型回合中查看，已从活跃上下文释放；"
                        "原始像素快照仍保留在轨迹中，上一回合的视觉结论继续有效。"
                        "不要因这条上下文维护提示重新查看同一未变化文件；"
                        "仅在图片被重生、替换或修图后检查新像素。]"
                    ),
                })
                block["content"] = next_content
    return released


def _role_contract_closeout_issues(agent, kind, contract):
    """Return missing/invalid fields that the same child can still restate.

    This does not decide quality and never converts a blocked result to ready.
    It only prevents a worker that already stopped from losing an otherwise
    valid run because its final machine-readable handoff omitted required keys.
    """
    if kind == "research":
        declared = str(contract.get("output") or "").strip()
        for relative in dict.fromkeys(
                value for value in (declared, "research/research.md") if value):
            candidate = os.path.abspath(os.path.join(agent.ws, relative))
            try:
                inside = os.path.commonpath([os.path.abspath(agent.ws), candidate]) \
                    == os.path.abspath(agent.ws)
                if inside and os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                    return []
            except (OSError, ValueError):
                continue

    status = str(contract.get("status") or "").strip().lower()
    issues = []
    allowed_statuses = {
        "research": {"ready", "partial", "blocked"},
        "material": {"ready", "blocked"},
        "image": {"ready", "blocked"},
        "slide": {"ready", "blocked"},
        "review": {"ready", "blocked"},
    }.get(kind, {"ready", "blocked"})
    if status not in allowed_statuses:
        issues.append("status: " + " | ".join(sorted(allowed_statuses)))
    if status == "blocked":
        return issues
    if kind == "material":
        if not str(contract.get("coverage") or "").strip().lower().startswith("complete"):
            issues.append("coverage: complete")
    elif kind == "research":
        if status == "partial" and str(contract.get("unresolved") or "").strip().lower() in {
                "", "none", "n/a", "not-applicable"}:
            issues.append("unresolved")
        if not str(contract.get("output") or "").strip():
            issues.append("output")
    elif kind == "review":
        expected_mode = str(getattr(agent, "_expected_review_mode", "") or "final_review")
        if str(contract.get("mode") or "").strip() != expected_mode:
            issues.append(f"mode: {expected_mode}")
        if str(contract.get("content_fidelity") or "").strip() not in {
                "pass", "not-applicable", "fail"}:
            issues.append("content_fidelity")
        if expected_mode == "final_review" and str(
                contract.get("diagnosed_pages") or "").strip() != "all":
            issues.append("diagnosed_pages: all")
        if str(contract.get("final_pixels_inspected") or "").strip() not in {"yes", "no"}:
            issues.append("final_pixels_inspected")
        if str(contract.get("speech_aligned") or "").strip() not in {"yes", "no"}:
            issues.append("speech_aligned")
        if not str(contract.get("remaining") or "").strip():
            issues.append("remaining")
        try:
            int(str(contract.get("refine_rounds") or ""))
        except (TypeError, ValueError):
            issues.append("refine_rounds")
    return issues


def _review_ledger_contract(agent):
    """Recover a finished Review contract from this Review's canonical ledger.

    The ledger and the assistant handoff are two serializations of the same
    Review result.  A weak model can finish the former and then keep calling
    tools instead of emitting the latter.  Recover only from a ledger changed
    by *this* child and only when it explicitly says ``status: ready`` with no
    remaining/hard issue.  Pixel coverage and freshness remain independently
    enforced by the task-level acceptance gates.
    """
    if _task_kind(getattr(agent, "label", ""), agent.initial_user) != "review":
        return {}
    path = os.path.join(agent.ws, "_trace", "review-issues.md")
    try:
        with open(path, "rb") as stream:
            payload = stream.read()
        ledger_mtime_ns = os.stat(path).st_mtime_ns
    except OSError:
        return {}
    digest = hashlib.sha256(payload).hexdigest()
    initial_digest = str(getattr(agent, "_review_ledger_initial_sha256", "") or "")
    initial_mtime_ns = int(getattr(agent, "_review_ledger_initial_mtime_ns", 0) or 0)
    # A Review can legitimately rewrite an identical ready ledger.  Treat it as
    # fresh when the file timestamp advanced; reject only a truly untouched
    # ledger inherited from an earlier Review attempt.
    if digest == initial_digest and ledger_mtime_ns <= initial_mtime_ns:
        return {}
    text = payload.decode("utf-8", errors="ignore")
    contract = _final_contract(text)
    if str(contract.get("status") or "").strip().lower() != "ready":
        return {}

    # Never turn an explicit hard issue or unresolved item into ``ready``.
    remaining = str(contract.get("remaining") or "").strip().lower()
    if remaining and remaining not in {"none", "[]", "无", "无剩余问题"}:
        return {}
    hard_line = re.search(r"(?mi)^\s*(?:hard|硬伤|硬门问题)\s*[:：]\s*(.+?)\s*$", text)
    if hard_line and hard_line.group(1).strip().lower() not in {
            "none", "[]", "无", "没有", "0", "n/a", "not-applicable"}:
        return {}
    hard_lists = re.findall(r"(?mi)^\s*hard_issues\s*[:：]\s*(.+?)\s*$", text)
    if any(value.strip().lower() not in {"none", "[]", "无", "0"}
           for value in hard_lists):
        return {}

    # A ready ledger written before ``deck.py build`` is not a final Review.
    # Build may subset fonts, update base.css and re-render every page.  Recover
    # the missing assistant contract only when the persisted Vision evidence
    # still describes those exact post-build pixels.
    pixel_state = _review_pixel_state(agent)
    if (pixel_state["missing_pages"] or pixel_state["stale_pages"]
            or pixel_state["dirty_sources"]):
        return {}

    expected_mode = str(getattr(agent, "_expected_review_mode", "") or "final_review")
    recovered = dict(contract)
    recovered["status"] = "ready"
    recovered.setdefault("mode", expected_mode)
    recovered.setdefault("remaining", "none")
    recovered.setdefault("refine_rounds", str(
        int(getattr(agent, "_review_refine_rounds", 0) or 0)
    ))
    if int(getattr(agent, "n_vision_calls", 0) or 0) > 0:
        recovered.setdefault("final_pixels_inspected", "yes")
    if expected_mode == "final_review":
        recovered.setdefault("diagnosed_pages", "all")
    if os.path.isfile(os.path.join(agent.ws, "speech.md")):
        recovered.setdefault("speech_aligned", "yes")
    recovered.setdefault("content_fidelity", "not-applicable")
    recovered["contract_recovered_from"] = "_trace/review-issues.md"
    return recovered


def _contract_text(contract):
    """Serialize a compact recovered contract without altering the raw trace."""
    keys = (
        "status", "mode", "content_fidelity", "diagnosed_pages",
        "final_pixels_inspected", "speech_aligned", "remaining",
        "refine_rounds", "contract_recovered_from",
    )
    return "\n".join(
        f"{key}: {contract[key]}" for key in keys if key in contract
    )


def _history_tool_names(messages):
    """Return tool_use id -> name for compact summaries without changing tool pairing."""
    names = {}
    for message in messages:
        for block in (message.get("content") if isinstance(message, dict) else []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names[str(block.get("id") or "")] = str(block.get("name") or "tool")
    return names


def _compact_result_text(text, tool_name):
    """Keep actionable evidence from an old tool result, not its full transport payload."""
    value = str(text or "")
    if len(value) <= HISTORY_TOOL_RESULT_MAX_CHARS or "[历史工具结果已压缩]" in value:
        return value
    # delegate_task is already a compact, structured handoff.  Keeping it whole avoids
    # sending the parent back into child traces to recover a dropped contract field.
    if tool_name == "delegate_task":
        return value
    paths = []
    for path in re.findall(
        r"(?:^|[\s'\"`])((?:plan|research|materials|assets|slides|renders|_trace)/[^\s'\"`]+)",
        value,
        re.I,
    ):
        clean = path.rstrip(".,;:)。，；：）")
        if clean not in paths:
            paths.append(clean)
    signals = []
    for line in value.splitlines():
        if re.search(
            r"(?i)\b(status|error|failed|failure|warning|blocked|timeout|exit|coverage|unresolved)\b|"
            r"错误|失败|警告|阻塞|超时|未解决|覆盖",
            line,
        ):
            stripped = line.strip()
            if stripped and stripped not in signals:
                signals.append(stripped[:300])
            if len(signals) >= 6:
                break
    head = value[:500].strip()
    tail = value[-350:].strip() if len(value) > 850 else ""
    parts = [f"[历史工具结果已压缩] tool={tool_name}; original_chars={len(value)}"]
    if paths:
        parts.append("artifacts: " + ", ".join(paths[:12]))
    if signals:
        parts.append("signals:\n" + "\n".join(signals))
    if head:
        parts.append("excerpt_start: " + head)
    if tail and tail != head:
        parts.append("excerpt_end: " + tail)
    return "\n".join(parts)


def _compact_active_history(messages):
    """Compact early ordinary text/tool output while leaving the immutable Trace untouched.

    Tool-use blocks and signed thinking blocks are never removed or rewritten.  This keeps
    Anthropic tool pairing valid and preserves reasoning signatures while preventing old
    searches, terminal errors and large read_file results from dominating every later call.
    """
    try:
        size = len(json.dumps(messages, ensure_ascii=False, default=str))
    except Exception:
        return 0
    if size <= HISTORY_COMPACT_AFTER_CHARS:
        return 0
    names = _history_tool_names(messages)
    cutoff = max(1, len(messages) - HISTORY_KEEP_RECENT_MESSAGES)
    saved = 0
    for index, message in enumerate(messages[:cutoff]):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_name = names.get(str(block.get("tool_use_id") or ""), "tool")
                old = block.get("content")
                if isinstance(old, str):
                    new = _compact_result_text(old, tool_name)
                    saved += max(0, len(old) - len(new))
                    block["content"] = new
                elif isinstance(old, list):
                    for item in old:
                        if isinstance(item, dict) and item.get("type") == "text":
                            prior = str(item.get("text") or "")
                            new = _compact_result_text(prior, tool_name)
                            saved += max(0, len(prior) - len(new))
                            item["text"] = new
            elif block.get("type") == "text":
                old = str(block.get("text") or "")
                if len(old) > HISTORY_TEXT_MAX_CHARS and "[早期助手文本已压缩]" not in old:
                    new = (
                        f"[早期助手文本已压缩; original_chars={len(old)}] "
                        + old[:700].strip()
                        + (" … " + old[-220:].strip() if len(old) > 920 else "")
                    )
                    saved += max(0, len(old) - len(new))
                    block["text"] = new
    return saved


def _run_tools(agent, tool_uses, turn, tool_log):
    """执行一个回合的所有 tool_use;每个 tool_use **一定**配一个 tool_result(即使失败)。
    同一回合里:**多个 delegate_task 并行**;**多个纯 IO 叶子工具(PARALLEL_LEAF_TOOLS,如 image_generate)
    也并行**(单独限并发 TURN_TOOL_PARALLEL 防出图/搜索网关过载);其余工具(会改 agent 记账 / 需保序)顺序执行。
    并行工具均无 agent 状态记账、内容寻址落盘不撞名 → 线程安全;结果按原 index 回填,顺序不乱。"""
    for tu in tool_uses:
        args = tu.input if isinstance(tu.input, dict) else {}
        agent.log(f"🔧 {tu.name}({json.dumps(args, ensure_ascii=False)[:130]})")
        tool_log.append({"turn": turn, "name": tu.name, "args": args})
    results = [None] * len(tool_uses)
    deleg = [i for i, tu in enumerate(tool_uses) if tu.name == "delegate_task"]
    leaf = [i for i, tu in enumerate(tool_uses) if tu.name in PARALLEL_LEAF_TOOLS]
    par = set(deleg) | set(leaf)
    for i, tu in enumerate(tool_uses):                 # 非并行工具:顺序(含会改 agent 状态的记账)
        if i not in par:
            results[i] = _exec_one_tool(agent, tu)
    if len(deleg) == 1:                                # delegate:并行(并发由父级 _child_sem 统一卡)
        results[deleg[0]] = _exec_one_tool(agent, tool_uses[deleg[0]])
    elif len(deleg) > 1:
        with cf.ThreadPoolExecutor(max_workers=len(deleg), thread_name_prefix="deleg") as ex:
            futs = {ex.submit(_exec_one_tool, agent, tool_uses[i]): i for i in deleg}
            for f in cf.as_completed(futs):
                results[futs[f]] = f.result()
    if len(leaf) == 1:                                 # 纯 IO 叶子:单个直接跑
        results[leaf[0]] = _exec_one_tool(agent, tool_uses[leaf[0]])
    elif len(leaf) > 1:                                # 多个纯 IO 叶子:并行,单独限并发防网关过载
        with cf.ThreadPoolExecutor(max_workers=min(len(leaf), TURN_TOOL_PARALLEL), thread_name_prefix="leaf") as ex:
            futs = {ex.submit(_exec_one_tool, agent, tool_uses[i]): i for i in leaf}
            for f in cf.as_completed(futs):
                results[futs[f]] = f.result()
    return results


def run_loop(agent):
    """纯 ReAct 循环:模型 → 工具 → 模型,直到模型不再调用工具(自然收尾)或到 max_turns。
    不替模型生成任务产物，也不在 max_turns 时强制总结；只对无 text/tool 的退化采样做有限重采，
    并在子角色遗漏最终合同或像素闭环时给同一角色一次收口机会。Harness 不会伪造 ready。
    同时保留传输/进程层安全(API 重试、子 agent 超时、子线程崩溃捕获)。写原始轨迹;
    返回 finished_clean。"""
    agent.trace.snapshot_inputs(agent.system, agent.tools, agent.config_snapshot())
    messages = [{"role": "user", "content": agent.initial_user}]
    # 轨迹使用独立的轻量历史：保留每次看图的 shot 引用，不保留 base64。
    # 活跃 messages 则在图像被消费后释放像素，使长 Deck 能持续分批审查。
    trace_messages = [copy.deepcopy(messages[0])]
    tool_log = []
    contract_closeouts = 0
    for turn in range(agent.max_turns):
        # 传输层有限重采: native-thinking 偶发
        # "只出 thinking 就 end_turn"的退化空采样(有 thinking、无 text、无 tool),当失败采样**丢弃并重采**
        # 最多 K 次，退化轮不 append、不进轨迹。无 tool 且无 text
        # 不可能是有效角色合同；无论是 thinking-only 还是真空轮都重采。
        resp = None
        for _rs in range(RESAMPLE_THINKING_ONLY + 1):
            resp = _model_call(agent, messages)
            if resp is None:
                break
            _has_tool = any(b.type == "tool_use" for b in resp.content)
            _has_text = any(b.type == "text" and b.text.strip() for b in resp.content)
            _has_think = any(b.type == "thinking" and b.thinking.strip() for b in resp.content)
            if _has_tool or _has_text:
                break
            if _rs < RESAMPLE_THINKING_ONLY:
                flavor = "thinking-only" if _has_think else "empty"
                agent.log(f"[{turn}] ⟲ {flavor} 退化采样,丢弃重采 {_rs+1}/{RESAMPLE_THINKING_ONLY}")
        if resp is None:
            agent.exit_reason = "api_failed"
            agent.log("API 连续失败,放弃")
            break

        assistant_message = {"role": "assistant", "content": blocks_to_dicts(resp.content)}
        messages.append(assistant_message)
        trace_messages.append(copy.deepcopy(assistant_message))
        # 上一 user 回合中的图片已被这次响应消费；在执行新工具前就释放，
        # 下一次模型请求只携带新的视觉批次。
        released = _release_consumed_vision_images(messages)
        if released:
            agent.log(f"[视觉上下文] 已释放 {released} 张已消费图像，轨迹快照仍保留")
        turn_text = ""
        for b in resp.content:
            if b.type == "thinking" and b.thinking.strip():
                agent.log(f"[{turn}] 🧠 {b.thinking.strip()[:140]}")
            if b.type == "text" and b.text.strip():
                agent.log(f"[{turn}] 💬 {b.text.strip()[:180]}")
                turn_text = b.text.strip()
                agent.final_text = turn_text
        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        if not tool_uses:
            kind = _task_kind(getattr(agent, "label", ""), agent.initial_user)
            contract = _final_contract(turn_text)
            contract_issues = _role_contract_closeout_issues(agent, kind, contract)
            needs_contract = (
                getattr(agent, "role", "") != "orchestrator"
                and kind in {"material", "research", "image", "slide", "review"}
                and bool(contract_issues)
            )
            pixel_state = None
            if kind == "slide" and getattr(agent, "role", "") != "orchestrator":
                pixel_state = _slide_pixel_state(
                    agent, getattr(agent, "_assigned_pages", [])
                )
            elif kind == "review" and getattr(agent, "role", "") != "orchestrator":
                pixel_state = _review_pixel_state(agent)
            needs_pixel_closeout = bool(
                pixel_state
                and (
                    pixel_state["missing_pages"]
                    or pixel_state["stale_pages"]
                    or pixel_state["dirty_sources"]
                )
            )
            if ((needs_contract or needs_pixel_closeout)
                    and contract_closeouts < CONTRACT_CLOSEOUT_RETRIES
                    and turn + 1 < agent.max_turns):
                contract_closeouts += 1
                language = str(
                    getattr(agent, "prompt_language", "zh") or "zh"
                ).lower()
                if needs_pixel_closeout:
                    missing = ",".join(
                        f"{page:02d}" for page in pixel_state["missing_pages"]
                    ) or "none"
                    stale = ",".join(
                        f"{page:02d}" for page in pixel_state["stale_pages"]
                    ) or "none"
                    dirty = ",".join(pixel_state["dirty_sources"][:8]) or "none"
                    if kind == "review" and language == "en":
                        reminder = (
                            "Your Review stopped before its post-build final-pixel proof was "
                            f"current. Missing final pages: {missing}; stale pixels: {stale}; "
                            f"unrendered visual sources: {dirty}. Run the deterministic build "
                            "first, regenerate renders/review-contact.json and its contact sheets, "
                            "then inspect the new final contact sheets (or final page PNGs) with "
                            "vision_analyze. Update _trace/review-issues.md only after that final "
                            "inspection, make no further visual edit/build, and return the exact "
                            "Review contract. Do not claim ready before the final pixels are current."
                        )
                    elif kind == "review":
                        reminder = (
                            "你的 Review 在 build 后最终像素证据完整前停止了。"
                            f"缺少最终页：{missing}；像素已过期：{stale}；"
                            f"尚未重渲源文件：{dirty}。请先执行确定性 build，再重新生成 "
                            "renders/review-contact.json 及联系表，然后用 vision_analyze 查看新生成的"
                            "最终联系表（或最终单页 PNG）。最终看图后再更新 _trace/review-issues.md，"
                            "之后不得继续修改视觉文件或 build，最后按角色卡返回 Review 合同。"
                            "最终像素新鲜前不得返回 ready。"
                        )
                    elif language == "en":
                        reminder = (
                            "Your Slide Group stopped before its final pixel proof was current. "
                            f"Missing direct page Vision: {missing}; stale pixels: {stale}; "
                            f"unrendered visual sources: {dirty}. Render every affected page, "
                            "run vision_analyze on each final renders/slide_NN.png, make no edit "
                            "after that inspection, then return the exact Slide role contract. "
                            "Do not claim ready until this is complete."
                        )
                    else:
                        reminder = (
                            "你的 Slide Group 在最终像素证据完整前停止了。"
                            f"缺少逐页 Vision：{missing}；像素已过期：{stale}；"
                            f"尚未重渲源文件：{dirty}。请重渲所有受影响页面，逐页对最终 "
                            "renders/slide_NN.png 调用 vision_analyze；检查后不要再改 HTML/CSS，"
                            "然后按 Slide 角色卡返回准确合同。闭环完成前不得返回 ready。"
                        )
                elif language == "en":
                    reminder = (
                        "Your structured role contract is incomplete or invalid "
                        f"({'; '.join(contract_issues)}). If the assigned work "
                        "is incomplete, continue with the necessary tools now. If it is complete, "
                        "return the exact role-card contract with a standalone status field. Do "
                        "not describe planned work as completed."
                    )
                else:
                    reminder = (
                        "你刚才停止调用工具，但结构化角色合同缺失或无效（"
                        f"{'；'.join(contract_issues)}）。若任务尚未完成，请现在继续调用"
                        "必要工具；若已经完成，请按角色卡返回准确合同，至少包含独立的 status 字段。"
                        "不得把计划动作描述成已完成。"
                    )
                closeout_message = {"role": "user", "content": reminder}
                messages.append(closeout_message)
                trace_messages.append(copy.deepcopy(closeout_message))
                reason = "最终像素证据未闭环" if needs_pixel_closeout else "缺少结构化合同"
                agent.log(f"{kind} {reason}：进行第 {contract_closeouts} 次有限收口提醒")
                continue
            # 有限合同提醒用完后，尊重模型的自然收尾：不代写产物、不伪造 ready。
            agent.exit_reason = "text_response" if turn_text else "stopped_no_text"
            agent.log(f"模型停止调用工具,收尾于回合 {turn}(stop={resp.stop_reason}, exit={agent.exit_reason})")
            break

        _tool_content = _run_tools(agent, tool_uses, turn, tool_log)
        recovered_review_contract = _review_ledger_contract(agent)
        review_recovery = _blocking_review_failure(agent)
        blocked_no_progress = sum(
            1
            for result in (_tool_content or [])
            if isinstance(result, dict)
            and "vision_analyze 已阻止" in str(result.get("content") or "")
        )
        if blocked_no_progress:
            agent._blocked_no_progress = int(
                getattr(agent, "_blocked_no_progress", 0) or 0
            ) + blocked_no_progress

        # 同一只读/联网动作反复出现不会产生新证据。第三次相同调用后停止当前
        # 子任务，避免 Research/Material 在一个错误路线中跑满数小时。
        repeatable = {"web_search", "web_extract", "fetch_image"}
        signatures = getattr(agent, "_repeatable_tool_calls", None)
        if not isinstance(signatures, dict):
            signatures = {}
            agent._repeatable_tool_calls = signatures
        repeated_calls = 0
        for tool_use in tool_uses:
            if tool_use.name not in repeatable:
                continue
            raw = json.dumps(tool_use.input if isinstance(tool_use.input, dict) else {},
                             ensure_ascii=False, sort_keys=True, default=str)
            signature = f"{tool_use.name}:{raw}"
            signatures[signature] = int(signatures.get(signature, 0)) + 1
            if signatures[signature] >= 3:
                repeated_calls += 1
        stalled = int(getattr(agent, "_blocked_no_progress", 0) or 0) >= 2 or repeated_calls
        finalization_only = bool(getattr(agent, "_finalization_only", False))
        label_lower = str(getattr(agent, "label", "") or "").lower()
        finalization_role = next(
            (kind for kind in ("material", "research", "image", "review", "slide")
             if label_lower.startswith(kind)),
            "",
        )
        role_can_finalize = bool(finalization_role)
        stop_after_results = False
        if review_recovery:
            # Review blocked means bounded repair/recheck, not whole-deck failure.
            if isinstance(_tool_content, list):
                _tool_content = _tool_content + [{
                    "type": "text", "text": review_recovery["instruction"]
                }]
            agent.log(
                f"Review {review_recovery['status']}：进入有限重派/复验流程；"
                "预算耗尽后保留问题账本并交付可用成稿。"
            )
        elif stalled and role_can_finalize and not finalization_only:
            agent._finalization_only = True
            agent._finalization_role = finalization_role
            agent._stalled_finalize_rounds = 0
            agent._blocked_no_progress = 0
            language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
            if finalization_role == "review":
                instruction = (
                    "停滞保护已关闭继续看图、渲染和页面修改。只使用已经取得的新鲜像素证据，"
                    "把已发现问题与覆盖范围写入 _trace/review-issues.md；任务含附件、Research "
                    "或其他内容保真要求时，同时把逐页事实核验写入 _trace/content-fidelity.md。"
                    "收口阶段只允许补齐或更新这两份正式产物，然后按 Review "
                    "角色卡原样返回完整结构化合同。若全部页面已覆盖、最终像素仍新鲜且 remaining "
                    "为 none，应如实返回 status: ready；只有证据缺失或仍有真实问题时才返回 blocked。"
                    "不得虚构检查结果，也不得继续调用证据工具。"
                    if language != "en" else
                    "Stall protection has closed further vision, rendering, and page edits. "
                    "Use only the fresh pixel evidence already obtained, write the known issues "
                    "and inspected scope to _trace/review-issues.md. When the task has attachments, "
                    "Research, or another content-fidelity requirement, also write the page-level "
                    "fact verification to _trace/content-fidelity.md. Closeout may only complete or "
                    "update these two canonical artifacts. Then return the exact complete "
                    "Review contract from the role card. Return status: ready when all pages were "
                    "covered, final pixels are fresh, and remaining is none; return blocked only "
                    "for real unresolved issues or missing evidence. Do not fabricate evidence or "
                    "call more evidence tools."
                )
            elif finalization_role == "slide":
                instruction = (
                    "停滞保护已关闭继续看图、渲染和修改。只使用已有产物与新鲜像素证据，按 Slide "
                    "角色卡原样返回完整结构化合同。所属页面、渲染和最终像素证据均完整且无硬伤时"
                    "如实返回 status: ready；否则返回 blocked 并列明真实缺口。不得继续调用工具。"
                    if language != "en" else
                    "Stall protection has closed further vision, rendering, and edits. Use only "
                    "existing artifacts and fresh pixel evidence, then return the exact complete "
                    "Slide contract from the role card. Return status: ready when assigned pages, "
                    "renders, and final pixels are complete and clean; otherwise return blocked "
                    "with the real gaps. Do not call more tools."
                )
            elif finalization_role == "image":
                instruction = (
                    "停滞保护已关闭继续搜图、生图、下载和看图。根据已有素材文件与 catalog 状态，"
                    "按 Image 角色卡原样返回完整结构化合同：计划素材均已就绪且路径存在时返回 "
                    "status: ready；否则返回 blocked 并逐项列明缺口。不得继续调用工具或虚构素材。"
                    if language != "en" else
                    "Stall protection has closed further search, generation, download, and vision. "
                    "Use the existing asset files and catalog state to return the exact complete "
                    "Image contract from the role card: status: ready only when every planned asset "
                    "exists and is ready; otherwise return blocked with explicit gaps. Do not call "
                    "more tools or invent assets."
                )
            else:
                instruction = (
                    "停滞保护已关闭继续检索/看图。现在只允许把已经取得的证据写入角色要求的正式产物，"
                    "然后按角色卡返回结构化合同；证据不足请返回 partial 或 blocked，不得继续调用证据工具。"
                    if language != "en" else
                    "Stall protection has closed further search and vision. Use only the evidence "
                    "already obtained: write the required canonical artifact, then return the role "
                    "contract. If evidence is insufficient, return partial or blocked."
                )
            if isinstance(_tool_content, list):
                _tool_content = _tool_content + [{"type": "text", "text": instruction}]
            agent.log("停滞保护触发：进入一次受控收口，不再允许继续检索或看图。")
        elif finalization_only:
            agent._stalled_finalize_rounds = int(
                getattr(agent, "_stalled_finalize_rounds", 0) or 0
            ) + 1
            # A closeout response with no tools exits naturally before this
            # branch.  Allow several correction turns for a weak model that
            # initially emits a forbidden tool call, but keep a finite bound.
            stop_after_results = agent._stalled_finalize_rounds >= 4
        elif stalled:
            stop_after_results = True
        if THINK_NUDGE_EACH_TOOL and isinstance(_tool_content, list):
            # 在 tool_result 块之后追加一句引导思考的 text(合法:user 回合可 tool_result+text 并存)
            _tool_content = _tool_content + [{"type": "text", "text": THINK_NUDGE_TEXT}]
        tool_message = {"role": "user", "content": _tool_content}
        messages.append(tool_message)
        # clean() 将图像替换为 shot 路径，避免轨迹内存持有第二份 base64。
        trace_messages.append(agent.trace.clean(tool_message))
        compacted = _compact_active_history(messages)
        if compacted:
            agent.log(f"[上下文维护] 已压缩早期普通文本/工具结果约 {compacted} 字符；原始 Trace 未改变")
        if recovered_review_contract:
            agent.final_text = _contract_text(recovered_review_contract)
            agent._recovered_review_contract = recovered_review_contract
            agent.exit_reason = "review_ledger_ready"
            agent.log(
                "Review 已在本轮问题账本中明确 ready；Harness 恢复最终合同并停止继续调用工具。"
            )
            break
        if stop_after_results:
            agent.exit_reason = "stalled_repetition"
            agent.log(
                "停滞保护触发：相同工具路线或未变化像素被重复请求；"
                "停止当前子任务，保留已有产物并避免继续膨胀上下文。"
            )
            break
    else:
        agent.exit_reason = "max_turns"
        agent.log(f"到达 max_turns({agent.max_turns}),停止")

    # 纯 loop:到 max_turns 直接停,**不做强制总结**——模型怎么收(或没收)就怎么记,真实暴露其能力。
    finished_clean = agent.exit_reason in {"text_response", "review_ledger_ready"}
    agent.trace.write(trace_messages, tool_log)
    _write_usage(agent)   # ① 落 usage.json
    agent.nova_precheck = nova_bridge.finalize_agent(
        agent, messages, finished_clean
    )
    agent.log(f"轨迹已写: turns={len(tool_log)} renders={agent.n_renders} exit={agent.exit_reason}")
    return finished_clean


# ============= 委派子 agent(自由函数,操作一个 agent) =============

def _normalize_task(t):
    """归一化 Long-Horizon Presenter 的子任务并补齐角色必需能力。"""
    if not isinstance(t, dict):
        t = {"goal": str(t)}
    goal = str(t.get("goal") or "")
    label = str(t.get("label") or "").strip()
    derived_label = _label_from_goal(goal)
    # Bracket role tags are the paired Skill's semantic identity.  Prefer them
    # over a generic model-supplied child_NN label so traces stay intelligible.
    if not label or re.fullmatch(r"child[_-]?\d+", label, re.I) or re.search(
            r"\[\s*(?:research|material|image|slide|review)(?:[\s_-]+[^\]]+)?\s*\]",
            goal, re.I):
        label = derived_label or label
    toolsets = tools.normalize_toolset_names(
        t.get("toolsets") or ["file", "terminal", "vision"]
    )
    kind = _task_kind(label, goal)
    required_toolsets = {
        "research": ["file", "web"],
        "material": ["file", "terminal", "vision"],
        "image": ["file", "terminal", "web", "image_gen", "vision"],
        "slide": ["file", "terminal", "vision"],
        "review": ["file", "terminal", "vision"],
    }
    # Dedicated pairing: a role must not lose a required capability because
    # the Orchestrator omitted one field in a long delegate_task payload.
    for required in required_toolsets.get(kind, []):
        if required not in toolsets:
            toolsets.append(required)
    if kind == "slide":
        # Slide workers consume the frozen asset manifest; they are not a
        # second, implicit Image stage.  Strip both acquisition toolsets even
        # when a model copied them from its parent delegate payload.
        toolsets = [name for name in toolsets if name not in {"image_gen", "web"}]
    normalized = {
        "goal": goal,
        "context": t.get("context", ""),
        "toolsets": toolsets,
        "role": t.get("role", "leaf"),
        "label": label,
    }
    assigned_pages = t.get("assigned_pages")
    if isinstance(assigned_pages, (list, tuple, set)):
        pages = sorted({
            int(page) for page in assigned_pages
            if str(page).strip().isdigit() and int(page) > 0
        })
        if pages:
            normalized["assigned_pages"] = pages
    return normalized


def _task_kind(label, goal):
    """Resolve role identity from explicit signals only.

    Priority: label prefix > bracket role tag > an explicit role header on the
    FIRST non-empty line (e.g. ``Slides: build ...`` / ``Review: ...``).  Incidental
    occurrences of ``Slides:`` / ``Review:`` deeper inside the prose must never
    decide the role — otherwise an unlabelled Image task that merely mentions
    "for Slides: 1-3" would be misclassified and lose its image_gen/web tools.
    """
    label_text = str(label or "").strip().lower()
    goal_text = str(goal or "")
    for prefix, kind in (
        ("slide", "slide"), ("image", "image"), ("research", "research"),
        ("material", "material"), ("review", "review"),
    ):
        if label_text.startswith(prefix):
            return kind
    tagged = re.search(
        r"\[\s*(research|material|image|slide|review)(?:[\s_-]+[^\]]+)?\s*\]",
        goal_text, re.I,
    )
    if tagged:
        return tagged.group(1).lower()
    # Explicit role header, but only on the first non-empty line.
    first_line = ""
    for line in goal_text.splitlines():
        if line.strip():
            first_line = line.strip()
            break
    head = first_line.lower()
    if re.match(r"slides?(?:\s+group|\s+\d|\s*:)", head):
        return "slide"
    if re.match(r"image\s*:", head):
        return "image"
    if re.match(r"research\s*:", head):
        return "research"
    if re.match(r"material\s*:", head):
        return "material"
    if (re.match(r"(?:final\s+)?review\s*:", head)
            or re.match(r"mode\s*=\s*(?:final_review|simple_edit)\b", head)):
        return "review"
    return "other"


def _role_card_context(skills_root, kind, prompt_language="zh", skill_name=""):
    """Point a child at exactly one role card; the child reads it autonomously."""
    if kind not in {"research", "material", "image", "slide", "review"}:
        return ""
    selected_skill = str(skill_name or "").strip() or (
        "sn-ppt-web-en" if str(prompt_language or "").lower() == "en"
        else "sn-ppt-web-zh"
    )
    path = f"skills/{selected_skill}/subagents/{kind}.md"
    if str(prompt_language or "").lower() == "en":
        return (
            f"Your only role-card path is {path}. Before taking any task action, "
            "read the whole file yourself with read_file. If the result provides a "
            "continuation offset, keep reading until it is no longer truncated. Read "
            "only the references routed by that role card; do not scan unrelated files. "
            "The Harness validates only the final contract and does not inject the card "
            "body here.\n\n"
        )
    return (
        f"你的唯一角色卡路径是 {path}。"
        "开始任何任务动作前，必须自行用 read_file 完整读取该文件；若返回续读 offset，"
        "继续读取到不再截断。只按角色卡给出的路由读取必要 reference，不通读无关文件。"
        "Harness 只校验最终合同，不会在此重复角色卡正文。\n\n"
    )


def _label_from_goal(goal):
    """给漏传 label 的常见职责生成稳定名称，避免 child_01 轨迹。"""
    text = str(goal or "").strip()
    low = text.lower()
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
        if suffix:
            return f"{role}_{suffix}"
        return role
    marker = re.search(
        r"\[(research|material|image|slide)(?:[_-](\d{1,3}))?\]|\[(review)\]",
        text,
        re.I,
    )
    if marker:
        role = (marker.group(1) or marker.group(3)).lower()
        return f"{role}_{int(marker.group(2)):02d}" if marker.group(2) else role
    if re.match(r"slide\s+group\b", low):
        match = re.match(r"slide\s+group\s+([^\s:\[]+)", text, re.I)
        suffix = re.sub(r"[^a-zA-Z0-9_-]+", "-", match.group(1)).strip("-") if match else "group"
        return f"slide_group_{suffix.lower()}"
    if re.match(r"slides?(?:\s+\d|\s*:)", low):
        match = re.search(r"\b(\d{1,3})\b", text)
        return f"slide_{int(match.group(1)):02d}" if match else "slide"
    if low.startswith("final review"):
        return "review"
    for prefix in ("review", "image", "research", "material"):
        if low.startswith(prefix):
            return prefix
    return None


def _parse_page_list(raw):
    pages = set()
    value = re.sub(r"(?i)\b(?:p|page)\s*(?=\d)", "", str(raw or ""))
    value = re.sub(r"\s*(?:至|到|~|〜)\s*", "-", value)
    for part in re.split(r"\s*[,，、/;；]\s*|\s+", value.strip()):
        if not part:
            continue
        span = re.fullmatch(r"0?(\d{1,3})\s*[-–—]\s*0?(\d{1,3})", part)
        if span:
            start, end = map(int, span.groups())
            if 0 < start <= end <= 999:
                pages.update(range(start, end + 1))
        elif re.fullmatch(r"0?\d{1,3}", part) and int(part) > 0:
            pages.add(int(part))
    return pages


def _slide_group_pages(task):
    """从 Slide / Slide Group 委派文本中读取明确负责的页码。

    Skill 的标准标题是 ``Slide Group <id> [01, 02, ...]``。若标题缺失，
    再从明确的 ``plan/slide_NN.md`` 交接路径恢复；不从字号、颜色或尺寸中猜数字。
    """
    structured_pages = task.get("assigned_pages")
    if isinstance(structured_pages, (list, tuple, set)):
        pages = {
            int(page) for page in structured_pages
            if str(page).strip().isdigit() and int(page) > 0
        }
        if pages:
            return sorted(pages)
    text = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
    pages = set()
    explicit_patterns = (
        r"slide[ \t]+group\b[^\[\n]*\[[ \t]*([0-9pP \t,，、/;；\-–—~〜至到]+)[ \t]*\]",
        r"(?i)\bpages?[ \t]*[:：]?[ \t]*([0-9pP \t,，、/;；\-–—~〜至到]+)",
        r"(?:页码|负责页面|处理页面)[ \t]*[:：]?[ \t]*([0-9pP \t,，、/;；\-–—~〜至到]+)",
        r"(?:负责|处理|制作|完成)[ \t]*第?[ \t]*([0-9pP \t,，、/;；\-–—~〜至到]+)[ \t]*页(?:面)?",
        r"第[ \t]*([0-9pP \t,，、/;；\-–—~〜至到]+)[ \t]*页(?:面)?",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            pages.update(_parse_page_list(match.group(1)))
            if pages:
                break
    if not pages:
        pages.update(int(n) for n in re.findall(r"plan/slide_(\d{1,3})\.md\b", text, re.I))
    if not pages:
        single = re.search(r"\bslide\s+0?(\d{1,3})\b", text, re.I)
        if single and int(single.group(1)) > 0:
            pages.add(int(single.group(1)))
    return sorted(pages)


def _slide_group_page_count(task):
    """Return the number of explicitly assigned pages for budget scaling."""
    pages = _slide_group_pages(task)
    return max(1, len(pages))


def _slide_pixel_state(agent, assigned_pages):
    """Return machine-derived final-pixel coverage for one Slide worker.

    A page is current only when the worker inspected that exact final PNG and
    neither its HTML nor the shared CSS is newer than the render.  This helper
    is used both before the worker exits (so it can self-correct once) and by
    the parent acceptance record (so the correction cannot be self-reported).
    """
    assigned = sorted({int(page) for page in (assigned_pages or []) if int(page) > 0})
    inspected = sorted({
        int(match.group(1))
        for path in getattr(agent, "vision_paths", [])
        for match in [re.search(
            r"(?:^|/)renders/slide_(\d+)\.png$",
            str(path).replace("\\", "/"), re.I,
        )]
        if match and (not assigned or int(match.group(1)) in assigned)
    })
    evidence = getattr(agent, "vision_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}
    stale = []
    for page in inspected:
        rel = f"renders/slide_{page:02d}.png"
        item = next(
            (value for path, value in evidence.items()
             if str(path).replace("\\", "/").endswith(rel)),
            None,
        )
        png = os.path.join(agent.ws, rel)
        html = os.path.join(agent.ws, "slides", f"slide_{page:02d}.html")
        css = os.path.join(agent.ws, "base.css")
        try:
            if not os.path.isfile(html):
                raise OSError("missing slide HTML")
            with open(png, "rb") as stream:
                current_digest = hashlib.sha256(stream.read()).hexdigest()
            current_mtime = os.stat(png).st_mtime_ns
            source_mtime = max(
                (os.stat(path).st_mtime_ns for path in (html, css) if os.path.isfile(path)),
                default=0,
            )
        except OSError:
            stale.append(page)
            continue
        if (not isinstance(item, dict)
                or item.get("sha256") != current_digest
                or int(item.get("mtime_ns") or 0) != current_mtime
                or source_mtime > current_mtime):
            stale.append(page)
    dirty = sorted(
        os.path.relpath(path, agent.ws).replace(os.sep, "/")
        for path in getattr(agent, "_dirty_visual_sources", set())
    )
    return {
        "inspected_pages": inspected,
        "missing_pages": sorted(set(assigned) - set(inspected)),
        "stale_pages": sorted(set(stale)),
        "dirty_sources": dirty,
    }


def _review_pixel_state(agent):
    """Return machine-derived final-pixel coverage for a full-deck Review.

    Contact-sheet evidence is expanded through ``review-contact.json``.  The
    viewed image bytes and mtime must still match, and every slide render must
    be newer than both its HTML and shared CSS.  This keeps a pre-build Review
    from being recovered as ready after font bundling changed final pixels.
    """
    expected_mode = str(
        getattr(agent, "_expected_review_mode", "") or "final_review"
    )
    workspace = str(getattr(agent, "ws", "") or "")
    if not workspace:
        # Lightweight protocol/unit-test agents do not own a workspace.  The
        # task-level acceptance gate still enforces real Review freshness.
        return {
            "inspected_pages": [], "missing_pages": [], "stale_pages": [],
            "dirty_sources": [],
        }
    if expected_mode != "final_review":
        return {
            "inspected_pages": [], "missing_pages": [], "stale_pages": [],
            "dirty_sources": [],
        }
    manifest_path = os.path.join(workspace, "renders", "review-contact.json")
    try:
        with open(manifest_path, encoding="utf-8") as stream:
            full = (json.load(stream).get("full") or {})
    except (OSError, ValueError, TypeError):
        expected = []
        slides_dir = os.path.join(workspace, "slides")
        try:
            for name in os.listdir(slides_dir):
                match = re.fullmatch(r"slide_(\d+)\.html", name)
                if match:
                    expected.append(int(match.group(1)))
        except OSError:
            pass
        return {
            "inspected_pages": [],
            "missing_pages": sorted(set(expected)) or [0],
            "stale_pages": [],
            "dirty_sources": [],
        }
    expected = {int(page) for page in full.get("pages") or [] if int(page) > 0}
    groups = {
        str(item.get("path") or "").replace("\\", "/").lstrip("./"): {
            int(page) for page in item.get("pages") or []
        }
        for item in full.get("groups") or [] if isinstance(item, dict)
    }
    evidence = getattr(agent, "vision_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}

    def normalize(path):
        raw = str(path or "")
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, workspace)
        return raw.replace("\\", "/").lstrip("./")

    current = {}
    for raw, item in evidence.items():
        rel = normalize(raw)
        fp = os.path.join(workspace, rel)
        try:
            with open(fp, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            mtime_ns = os.stat(fp).st_mtime_ns
        except OSError:
            continue
        if (isinstance(item, dict) and item.get("sha256") == digest
                and int(item.get("mtime_ns") or 0) == mtime_ns):
            current[rel] = mtime_ns

    css = os.path.join(workspace, "base.css")
    css_mtime = os.stat(css).st_mtime_ns if os.path.isfile(css) else 0

    def page_is_fresh(page, viewed_mtime):
        html = os.path.join(workspace, "slides", f"slide_{page:02d}.html")
        png = os.path.join(workspace, "renders", f"slide_{page:02d}.png")
        try:
            source_mtime = max(os.stat(html).st_mtime_ns, css_mtime)
            png_mtime = os.stat(png).st_mtime_ns
        except OSError:
            return False
        return source_mtime <= png_mtime <= viewed_mtime

    covered = set()
    stale = set()
    for rel, viewed_mtime in current.items():
        pages = groups.get(rel)
        if pages is None:
            match = re.search(r"(?:^|/)renders/slide_(\d+)\.png$", rel, re.I)
            pages = {int(match.group(1))} if match else set()
        for page in pages:
            if page_is_fresh(page, viewed_mtime):
                covered.add(page)
            else:
                stale.add(page)
    dirty = sorted(
        os.path.relpath(path, workspace).replace(os.sep, "/")
        for path in getattr(agent, "_dirty_visual_sources", set())
    )
    return {
        "inspected_pages": sorted(covered),
        "missing_pages": sorted(expected - covered),
        "stale_pages": sorted(stale),
        "dirty_sources": dirty,
    }


def _slide_turn_budget(task):
    pages = _slide_group_page_count(task)
    return min(
        SLIDE_MAX_TURNS_BASE + SLIDE_MAX_TURNS_PER_EXTRA_PAGE * (pages - 1),
        SLIDE_MAX_TURNS_CAP,
    )


def _slide_timeout_budget(task):
    pages = _slide_group_page_count(task)
    return min(
        SLIDE_WORKER_TIMEOUT_BASE + SLIDE_WORKER_TIMEOUT_PER_EXTRA_PAGE * (pages - 1),
        SLIDE_WORKER_TIMEOUT_CAP,
    )


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

    toolsets = tools.normalize_toolset_names(
        task.get("toolsets") or ["file", "terminal", "vision"]
    )
    # role=orchestrator 且深度还允许再派时,才补 delegation 能力。
    if task.get("role") == "orchestrator" and parent._delegate_depth + 1 < MAX_SPAWN_DEPTH \
            and "delegation" not in toolsets:
        toolsets = toolsets + ["delegation"]
    schema = tools.resolve_toolsets(toolsets)
    have = {s["name"] for s in schema}             # 基础工具(read)默认并入
    schema = [tools.SCHEMAS[n] for n in tools.BASE_TOOL_NAMES if n not in have] + schema

    context = task.get("context") or ""
    # 自报身份(双管之一·训练信号):让子 agent 知道自己叫什么,并在收尾首句声明身份+产出。
    prompt_language = str(
        parent.cfg.get("_prompt_language") or getattr(parent, "prompt_language", "")
    ).lower()
    if prompt_language == "en":
        ident = (
            f"Your identity is {name}. Begin your final summary by naming your "
            "identity and deliverable.\n\n"
        )
        context_label = "Context"
    else:
        ident = f"你的身份是 {name};完成后在总结首句声明你的身份与产出。\n\n"
        context_label = "背景"
    kind = _task_kind(name, task.get("goal"))
    # Atomic start reservation BEFORE the Agent/Trace is built — the sub_dir path
    # is deterministic (subagents/<name>), so a crash between here and the
    # terminal record is still visible on restart (counted → next _rN, never a
    # silent same-name reuse).  The Agent below reuses this exact directory.
    _assigned_pages = _slide_group_pages(task) if kind == "slide" else []
    _reserved_sub_dir = os.path.join(parent.ws, "_trace", "subagents", name)
    _write_attempt_start(parent.ws, _reserved_sub_dir, name, base, c, kind, _assigned_pages)
    role_card = _role_card_context(
        parent.skills_root,
        kind,
        prompt_language,
        parent.cfg.get("_selected_skill_name"),
    )
    initial = ident + _child_language_contract(prompt_language) + role_card + task["goal"] + (
        f"\n\n{context_label}:\n{context}" if context else ""
    )
    # 叶子子 agent(无 delegation)不需大 max_tokens——降回省时,避免 32k 拖慢踩 WORKER_TIMEOUT(orch/可委派 child 保持大值)
    child_cfg = parent.cfg
    if "delegation" not in toolsets:
        _cap = min(int(parent.cfg.get("max_tokens", 16000)), SUBAGENT_MAX_TOKENS)
        child_cfg = dict(parent.cfg); child_cfg["max_tokens"] = _cap
    if name.startswith("slide"):
        child_cfg = dict(child_cfg)
        child_cfg["max_turns"] = _slide_turn_budget(task)
    child = Agent(role="subagent", sid=parent.sid, ws=parent.ws,
                  sub_dir=f"subagents/{name}", tools_schema=schema,
                  config=child_cfg, initial_user=initial, label=name,
                  system=getattr(parent, "child_system", parent.base_system),
                  skills_root=parent.skills_root,
                  forbid_write_prefixes=None)        # 子 agent 默认无写禁区(它们才是真正写产物的)
    child._delegate_depth = parent._delegate_depth + 1
    child._assigned_pages = _assigned_pages
    if kind == "review":
        review_text = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
        child._expected_review_mode = (
            "simple_edit" if re.search(r"\bmode\s*=\s*simple_edit\b", review_text, re.I)
            else "final_review"
        )
        ledger_path = os.path.join(parent.ws, "_trace", "review-issues.md")
        try:
            with open(ledger_path, "rb") as stream:
                ledger_payload = stream.read()
            child._review_ledger_initial_sha256 = hashlib.sha256(
                ledger_payload
            ).hexdigest()
            child._review_ledger_initial_mtime_ns = os.stat(ledger_path).st_mtime_ns
        except OSError:
            child._review_ledger_initial_sha256 = ""
            child._review_ledger_initial_mtime_ns = 0
    return child, name


# A transparent-subject REQUIREMENT, not a mention of the word "transparent".
# The plan/goal expresses the requirement with the authoritative field
# ``subject_only: true`` / ``presentation: subject-only`` or an explicit cutout
# instruction.  Matching the bare word ``transparent`` false-fires on physical
# material descriptions ("transparent acrylic water guides") and on the contract
# field name ``transparent_assets`` that every image task echoes back — both of
# which wrongly demanded an Alpha cutout and blocked Slide dispatch (deck 449).
_TRANSPARENCY_REQUEST_RE = re.compile(
    r"(?i)(?:"
    # Authoritative machine fields.
    r"subject[_ -]?only\s*[:=]\s*true"
    r"|presentation\s*[:=]\s*[`'\"]?subject[_ -]?only"
    r"|expect[_ -]?transparent\s*[:=]\s*true"
    r"|needs?[_ -]?(?:transparent|cutout|alpha)\s*[:=]\s*true"
    r"|transparent[_ -]background\s+(?:required|needed)"
    # Explicit CJK cutout semantics ONLY — the exact phrases that mean "deliver an
    # isolated subject / alpha cutout".  Deliberately NOT triggered by:
    #   · the echoed contract field name ``transparent_assets``
    #   · an English material description ("transparent acrylic")
    #   · a material-quality mention ``透明感`` / a loose ``要透明…质感``
    # while still triggering on the normal ``需要透明背景`` / ``主体需要透明``.
    r"|透明背景|背景透明"          # transparent background (as a cutout requirement)
    r"|主体透明|透明主体"          # transparent subject
    r"|主体需要透明|需要主体透明"
    r"|抠图|去背|去背景|退底|扣像"  # cut-out / knock-out verbs
    r")"
)
_TRANSPARENT_ASSETS_RE = re.compile(
    r"(?im)^\s*transparent_assets\s*:\s*(.+?)\s*$"
)


def _image_transparency_error(ws, task, name, contract, final_text, vision_paths):
    """Reject an Image ``ready`` that did not deliver verified Alpha assets.

    This is deliberately narrow: it only activates when the original Image task
    explicitly requests a transparent subject.  The paired Skill owns the image
    semantics; the Harness merely verifies the claimed local artifact and the
    fact that Image actually inspected that exact derived file.
    """
    if not str(name).lower().startswith("image"):
        return None
    request = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
    if not _TRANSPARENCY_REQUEST_RE.search(request):
        return None
    if str(contract.get("status") or "").lower() != "ready":
        return None
    match = _TRANSPARENT_ASSETS_RE.search(final_text or "")
    if match:
        raw_paths = [
            value.strip().strip("`'\"")
            for value in re.split(r"\s*[,，;；]\s*", match.group(1))
            if value.strip()
        ]
    else:
        # 弱模型偶尔会在自然语言里明确交付唯一 cutout，却漏了键名。
        # 允许从最终回复提取路径，但下方 Alpha/存在性/精确 Vision 校验一项不少。
        raw_paths = sorted(set(re.findall(
            r"(?<![A-Za-z0-9_.-])(assets/[A-Za-z0-9_./-]+-cutout\.png)",
            final_text or "",
        )))
        if not raw_paths:
            return "缺少 transparent_assets；需要透明主体的任务不能仅返回普通 RGB path"
    if not raw_paths or any(not path.endswith("-cutout.png") for path in raw_paths):
        return "transparent_assets 必须全部是 assets/*-cutout.png 派生文件"

    root = os.path.realpath(ws)
    inspected = {
        os.path.normpath(str(path).strip())
        for path in (vision_paths or [])
    }
    from PIL import Image
    for relative in raw_paths:
        normalized = os.path.normpath(relative)
        if normalized.startswith("../") or not normalized.startswith("assets/"):
            return f"透明素材路径不在 assets/：{relative}"
        absolute = os.path.realpath(os.path.join(root, normalized))
        if not absolute.startswith(root + os.sep) or not os.path.isfile(absolute):
            return f"透明素材不存在：{relative}"
        try:
            with Image.open(absolute) as image:
                if image.format != "PNG" or "A" not in image.getbands():
                    return f"透明素材不是带 Alpha 的 PNG：{relative}"
                alpha = image.getchannel("A")
                low, high = alpha.getextrema()
                if low >= 250 or high <= 16:
                    return f"透明素材没有有效的前景/透明区域：{relative}"
        except OSError as error:
            return f"透明素材无法解析：{relative}（{type(error).__name__}）"
        if normalized not in inspected and f"./{normalized}" not in inspected:
            return f"Image 未用 vision_analyze 查看最终透明素材：{relative}"
    return None


_HANDOFF_ARTIFACT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:research|plan|slides|renders|assets|materials)/"
    r"[^\s`'\"<>]+",
    re.I,
)


def _child_handoff(parent, child, name, clean, contract, accept_fields=None):
    """Persist the exact final response, but return only a lossless compact handoff.

    The parent needs verdict fields and artifact locations, not the child's full
    reasoning/tool history.  Keeping the exact final response in handoff.json
    avoids the old 240-character truncation without pushing messages.json back
    into the Orchestrator context.
    """
    final_text = child.final_text or ""
    artifacts = []
    for match in _HANDOFF_ARTIFACT_RE.finditer(final_text):
        value = match.group(0).rstrip(".,;，；:：)]}>")
        if value not in artifacts:
            artifacts.append(value)
    trace_rel = os.path.relpath(child.trace.sub_dir, parent.ws).replace(os.sep, "/")
    handoff_rel = f"{trace_rel}/handoff.json"
    base_label = re.sub(r"_r\d+$", "", str(name))
    attempt = 1
    attempt_match = re.search(r"_r(\d+)$", str(name))
    if attempt_match:
        attempt = int(attempt_match.group(1))
    payload = {
        "label": name,
        "clean": bool(clean),
        "exit_reason": child.exit_reason,
        "contract": contract,
        "artifacts": artifacts,
        "final_response": final_text,
        "vision_paths": list(getattr(child, "vision_paths", []) or []),
        "vision_evidence": dict(getattr(child, "vision_evidence", {}) or {}),
        # Self-describing fields so worker state can be rebuilt from disk after a
        # restart without the original task object (durable-state layer).
        "kind": _task_kind(name, None),
        "assigned_pages": list(getattr(child, "_assigned_pages", []) or []),
        "attempt": attempt,
        "base_label": base_label,
        "ts": time.time(),
    }
    # Persist the FULL machine-acceptance record so a late/recovered Slide or
    # Review handoff is not judged blind by the final acceptance consumers
    # (renders / vision_calls / inspected_pages / stale_pixel_pages /
    # dirty_visual_sources / machine_refine_rounds / trace_dir / shot).
    if accept_fields:
        for key in (
            "renders", "vision_calls", "trace_dir", "inspected_pages",
            "stale_pixel_pages", "dirty_visual_sources", "machine_refine_rounds",
            "shot", "nova_raw_precheck", "resume_of",
        ):
            if key in accept_fields:
                payload[key] = accept_fields[key]
    handoff_path = os.path.join(child.trace.sub_dir, "handoff.json")
    temporary = handoff_path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, handoff_path)
    _append_worker_ledger(parent.ws, {
        "ts": payload["ts"],
        "label": name,
        "base": base_label,
        "kind": payload["kind"],
        "attempt": attempt,
        "clean": bool(clean),
        "contract_status": str((contract or {}).get("status") or "").lower(),
        "assigned_pages": payload["assigned_pages"],
        "handoff": handoff_rel,
        "abandoned": False,
    })
    return {
        "label": name,
        "status": "ok" if clean else "issues",
        "exit_reason": child.exit_reason,
        "contract": contract,
        "artifacts": artifacts,
        "handoff_path": handoff_rel,
        "renders": child.n_renders,
        "vision_calls": child.n_vision_calls,
        "vision_evidence_path": f"{trace_rel}/vision-evidence.json",
        "shot": child.last_shot,
    }


def _write_attempt_start(ws, sub_dir, name, base, attempt, kind, assigned_pages):
    """Atomically record that an attempt has STARTED, before it runs.

    A crash between trace creation and the terminal handoff/failure would
    otherwise be invisible on restart, risking a silent same-name reuse.  The
    ``start.json`` reservation is a HARD requirement — if it cannot be atomically
    persisted, this raises and the caller MUST NOT construct the Agent (fail
    closed).  Only the advisory ledger append is best-effort.
    """
    os.makedirs(sub_dir, exist_ok=True)
    start_path = os.path.join(sub_dir, "start.json")
    payload = {
        "label": name, "base_label": base, "attempt": int(attempt),
        "kind": kind, "assigned_pages": list(assigned_pages or []),
        "ts": time.time(), "event": "start",
    }
    temporary = start_path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, start_path)   # atomic; raises on failure → fail closed
    _append_worker_ledger(ws, {
        "ts": time.time(), "label": name, "base": base, "kind": kind,
        "attempt": int(attempt), "clean": None, "contract_status": "",
        "assigned_pages": list(assigned_pages or []), "handoff": "",
        "event": "start",
    })


def _append_worker_ledger(ws, entry):
    """Append one JSONL event to _trace/worker-ledger.jsonl (advisory, never raises).

    Append-only sidesteps read-modify-write races: each worker thread only ever
    appends its own terminal event.  The reader tolerates a torn final line.
    """
    try:
        path = os.path.join(ws, "_trace", "worker-ledger.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        pass


def _read_worker_ledger(ws):
    """Return parsed ledger events, skipping any unparseable (torn) line."""
    path = os.path.join(ws, "_trace", "worker-ledger.jsonl")
    events = []
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return events


def _rebuild_worker_state(ws):
    """Rebuild the orchestrator's in-memory worker view from durable disk truth.

    Sources (preference order per concrete label): handoff.json (the worker
    actually finished and wrote real truth — this recovers a late/abandoned
    completion) > worker-failures/<label>.json > worker-ledger.jsonl.  Attempts
    are grouped by base label; the highest-attempt / latest attempt per base is
    ``active`` and older attempts are marked ``superseded_by`` it.  Returns a dict
    with worker_recs / spawn_count / role_spawn_count / slide_page_owners shaped
    exactly like the live in-memory structures so every consumer works unchanged.
    """
    by_label = {}   # concrete label -> rec dict

    def _consider(label, rec, priority):
        prior = by_label.get(label)
        if prior is None or priority > prior["_priority"]:
            rec["_priority"] = priority
            by_label[label] = rec

    # 1) durable handoffs (highest priority — real completion truth)
    for path in glob.glob(os.path.join(ws, "_trace", "subagents", "*", "handoff.json")):
        try:
            with open(path, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, TypeError):
            continue
        label = str(payload.get("label") or os.path.basename(os.path.dirname(path)))
        _consider(label, {
            "label": label,
            "_source": "handoff",
            "kind": str(payload.get("kind") or _task_kind(label, None)),
            "clean": bool(payload.get("clean")),
            "contract": payload.get("contract") or {},
            "assigned_pages": list(payload.get("assigned_pages") or []),
            "exit_reason": payload.get("exit_reason"),
            "vision_paths": list(payload.get("vision_paths") or []),
            "vision_evidence": dict(payload.get("vision_evidence") or {}),
            "attempt": int(payload.get("attempt") or _attempt_of(label)),
            "ts": float(payload.get("ts") or 0.0),
            # Full machine-acceptance record so a rebuilt late/recovered Slide or
            # Review rec is judged with real evidence, not blind defaults.
            "renders": int(payload.get("renders") or 0),
            "vision_calls": int(payload.get("vision_calls") or 0),
            "trace_dir": payload.get("trace_dir")
            or os.path.relpath(os.path.dirname(path), ws).replace(os.sep, "/"),
            "inspected_pages": list(payload.get("inspected_pages") or []),
            "stale_pixel_pages": list(payload.get("stale_pixel_pages") or []),
            "dirty_visual_sources": list(payload.get("dirty_visual_sources") or []),
            "machine_refine_rounds": int(payload.get("machine_refine_rounds") or 0),
            "shot": payload.get("shot"),
        }, priority=3)

    # 2) worker-failures (timeout/crash records)
    for path in glob.glob(os.path.join(ws, "_trace", "worker-failures", "*.json")):
        try:
            with open(path, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, TypeError):
            continue
        label = str(payload.get("label") or os.path.splitext(os.path.basename(path))[0])
        _consider(label, {
            "label": label,
            "_source": "failure",
            "kind": str(payload.get("kind") or _task_kind(label, None)),
            "clean": bool(payload.get("clean")),
            "contract": payload.get("contract") or {"status": "blocked"},
            "assigned_pages": list(payload.get("assigned_pages") or []),
            "exit_reason": payload.get("exit_reason") or "failed",
            "vision_paths": [],
            "vision_evidence": {},
            "attempt": int(payload.get("attempt") or _attempt_of(label)),
            "ts": float(payload.get("ts") or 0.0),
        }, priority=2)

    # 2.5) start markers — an attempt whose trace was created but never reached a
    # terminal handoff/failure (crash/interruption).  Lowest priority: a handoff
    # or failure for the same label overrides it.  Left as active-and-not-clean it
    # is an interrupted attempt, counted so restart issues the next _rN and never
    # silently reuses the same name.
    for path in glob.glob(os.path.join(ws, "_trace", "subagents", "*", "start.json")):
        try:
            with open(path, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError, TypeError):
            continue
        label = str(payload.get("label") or os.path.basename(os.path.dirname(path)))
        _consider(label, {
            "label": label,
            "_source": "start",
            "kind": str(payload.get("kind") or _task_kind(label, None)),
            "clean": False,
            "contract": {"status": "blocked", "validation_error": "interrupted (no terminal record)"},
            "assigned_pages": list(payload.get("assigned_pages") or []),
            "exit_reason": "interrupted",
            "vision_paths": [],
            "vision_evidence": {},
            "attempt": int(payload.get("attempt") or _attempt_of(label)),
            "ts": float(payload.get("ts") or 0.0),
            "interrupted": True,
        }, priority=0)

    # 3) ledger — fills kind/pages/ts for older handoffs and orders events
    for event in _read_worker_ledger(ws):
        label = str(event.get("label") or "")
        if not label:
            continue
        if str(event.get("event") or "") == "start":
            # A start event is only a reservation; the start.json scan above
            # already represents an orphan attempt.  A terminal ledger row (with
            # a real clean flag) for the same label supersedes it.
            continue
        existing = by_label.get(label)
        if existing is not None:
            if not existing.get("ts") and event.get("ts"):
                existing["ts"] = float(event.get("ts") or 0.0)
            if not existing.get("assigned_pages") and event.get("assigned_pages"):
                existing["assigned_pages"] = list(event.get("assigned_pages") or [])
            continue
        _consider(label, {
            "label": label,
            "_source": "ledger",
            "kind": str(event.get("kind") or _task_kind(label, None)),
            "clean": bool(event.get("clean")),
            "contract": {"status": str(event.get("contract_status") or "")},
            "assigned_pages": list(event.get("assigned_pages") or []),
            "exit_reason": "ledger",
            "vision_paths": [],
            "vision_evidence": {},
            "attempt": int(event.get("attempt") or _attempt_of(label)),
            "ts": float(event.get("ts") or 0.0),
        }, priority=1)

    recs = list(by_label.values())
    for rec in recs:
        rec.pop("_priority", None)
    # Group by base; latest attempt (then latest ts) per base is active.
    groups = {}
    for rec in recs:
        base = _base_label(rec["label"])
        rec["_base"] = base
        groups.setdefault(base, []).append(rec)
    spawn_count = {}
    slide_page_owners = {}
    role_spawn_count = {}
    for base, attempts in groups.items():
        active = max(attempts, key=lambda r: (
            int(r.get("attempt") or _attempt_of(r.get("label"))),
            float(r.get("ts") or 0.0),
            str(r.get("label") or ""),
        ))
        max_attempt = max(int(r.get("attempt") or _attempt_of(r.get("label"))) for r in attempts)
        spawn_count[base] = max_attempt
        for older in attempts:
            if older is active:
                continue
            older["superseded_by"] = active["label"]
            older["recovered"] = bool(active.get("clean"))
        kind = str(active.get("kind") or "")
        if kind in {"research", "review"}:
            # Budget by the HIGHEST attempt already allocated for this base (not the
            # count of records) so a sparse history — e.g. review_r3 with a missing
            # review_r2 — is charged as 3, never mis-counted as 1 and re-allowed.
            role_spawn_count[kind] = role_spawn_count.get(kind, 0) + max_attempt
        if kind == "slide":
            for page in active.get("assigned_pages") or []:
                try:
                    slide_page_owners[int(page)] = base
                except (TypeError, ValueError):
                    continue
    recs.sort(key=lambda r: (float(r.get("ts") or 0.0),
                             int(r.get("attempt") or _attempt_of(r.get("label"))),
                             str(r.get("label"))))
    for rec in recs:
        rec.pop("_base", None)
    return {
        "worker_recs": recs,
        "spawn_count": spawn_count,
        "role_spawn_count": role_spawn_count,
        "slide_page_owners": slide_page_owners,
    }


def _attempt_of(label):
    match = re.search(r"_r(\d+)$", str(label or ""))
    return int(match.group(1)) if match else 1


def _base_label(label):
    return re.sub(r"_r\d+$", "", str(label or ""))


def _effective_active_recs(worker_recs, *, kind=None, label_prefix=None):
    """Deterministically pick the effective ACTIVE attempt per base label.

    Shared by every acceptance/gate consumer so the choice never depends on input
    order or a stale ``superseded_by`` flag.  Within each base label the winner is
    the highest ``(attempt, ts, label)`` — recomputed here, not read from the
    record — so out-of-order input yields the same result.  Returns the active
    rec for each base, optionally filtered by kind and/or label prefix.
    """
    groups = {}
    for rec in worker_recs or []:
        label = str(rec.get("label") or "")
        if not label:
            continue
        groups.setdefault(_base_label(label), []).append(rec)
    active = []
    for base, attempts in groups.items():
        chosen = max(
            attempts,
            key=lambda r: (
                int(r.get("attempt") or _attempt_of(r.get("label"))),
                float(r.get("ts") or 0.0),
                str(r.get("label") or ""),
            ),
        )
        active.append(chosen)
    if kind is not None:
        active = [r for r in active if str(r.get("kind") or "").lower() == kind
                  or str(r.get("label") or "").lower().startswith(kind)]
    if label_prefix is not None:
        active = [r for r in active
                  if str(r.get("label") or "").lower().startswith(label_prefix)]
    active.sort(key=lambda r: (
        float(r.get("ts") or 0.0),
        int(r.get("attempt") or _attempt_of(r.get("label"))),
        str(r.get("label") or ""),
    ))
    return active


def _active_workers_from_disk(ws, kind_prefix, *, clean_source_only=False):
    """Return the *active* (effective per-base) rebuilt recs matching ``kind_prefix``.

    ``clean_source_only``: for a completion/clean judgement, a rec's ``clean`` flag
    may only be trusted when it came from a real ``handoff.json`` (``_source ==
    'handoff'``).  A ledger/start/failure-sourced ``clean`` is a recovery/diagnostic
    hint, never proof of completion.  When set, any non-handoff active rec is
    downgraded to not-clean so it can never fabricate a completed Image stage.
    """
    state = _rebuild_worker_state(ws)
    active = _effective_active_recs(state["worker_recs"], kind=kind_prefix)
    if clean_source_only:
        safe = []
        for rec in active:
            if rec.get("clean") and rec.get("_source") != "handoff":
                rec = dict(rec)
                rec["clean"] = False
            safe.append(rec)
        active = safe
    return active


def _hydrate_orchestrator_state(orch):
    """Rebuild worker_recs / spawn counts / page ownership from disk on restart.

    No-op for non-orchestrators, when the in-memory list is already populated
    (never clobber a live run), or when no durable worker artifacts exist.
    """
    if str(getattr(orch, "role", "") or "").lower() != "orchestrator":
        return
    if getattr(orch, "worker_recs", None):
        return
    ws = getattr(orch, "ws", None)
    if not ws:
        return
    has_state = (
        glob.glob(os.path.join(ws, "_trace", "subagents", "*", "handoff.json"))
        or glob.glob(os.path.join(ws, "_trace", "subagents", "*", "start.json"))
        or glob.glob(os.path.join(ws, "_trace", "worker-failures", "*.json"))
        or os.path.isfile(os.path.join(ws, "_trace", "worker-ledger.jsonl"))
    )
    if not has_state:
        return
    state = _rebuild_worker_state(ws)
    lock = getattr(orch, "_spawn_lock", None)
    if lock is None:
        lock = _NullContext()
    with lock:
        orch.worker_recs = state["worker_recs"]
        orch._spawn_count = dict(state["spawn_count"])
        orch._role_spawn_count = dict(state["role_spawn_count"])
        orch._slide_page_owners = dict(state["slide_page_owners"])


def _reconcile_worker_recs(orch):
    """Fold durable disk truth into the live in-memory worker_recs (idempotent).

    Called before every dispatch gate and before final acceptance so that a late
    completion (a timed-out/abandoned worker that later wrote a clean handoff),
    an out-of-band repaired handoff, or an interrupted-then-restarted attempt is
    reflected, without the orchestrator hand-editing memory.  Rules:
      - For a label present both in memory and on disk, the disk record wins when
        it is clean+ready and the memory record is not (late/repair recovery).
      - Disk-only labels (e.g. an orphan-start whose thread never rejoined) are
        appended so gates and acceptance can see and supersede them.
      - Never downgrade a live clean in-memory rec with stale disk data.
    Active/superseded is then recomputed by the shared grouping so every consumer
    reads a consistent view.  Thread-safe under _spawn_lock; safe to call often.
    """
    ws = getattr(orch, "ws", None)
    if not ws:
        return
    disk = _rebuild_worker_state(ws)
    disk_by_label = {str(r.get("label")): r for r in disk["worker_recs"]}
    lock = getattr(orch, "_spawn_lock", None) or _NullContext()
    with lock:
        mem = list(getattr(orch, "worker_recs", []) or [])
        merged = []
        seen = set()
        for rec in mem:
            label = str(rec.get("label"))
            seen.add(label)
            disk_rec = disk_by_label.get(label)
            mem_clean = bool(rec.get("clean"))
            # Trust ONLY a real handoff's machine clean flag for promotion — a
            # ledger-only "clean" (no handoff on disk) must not fabricate a
            # completion.  ``clean=True`` is the promotion signal, not a single
            # status enum: a late Research finishing clean+partial (or clean with
            # sparse contract fields but a valid brief) must also be promoted.
            disk_handoff_clean = bool(
                disk_rec
                and disk_rec.get("_source") == "handoff"
                and disk_rec.get("clean")
            )
            if disk_handoff_clean and not mem_clean:
                # Late/repair completion recovered from disk supersedes the stale
                # in-memory (abandoned/blocked) record for the same attempt.  Take
                # the disk record's fields (real machine-acceptance evidence) but
                # keep any live-only fields the memory rec had.
                promoted = dict(rec)
                promoted.update(disk_rec)
                merged.append(promoted)
            else:
                merged.append(rec)
        for label, disk_rec in disk_by_label.items():
            if label not in seen:
                merged.append(disk_rec)
        # Recompute active/superseded deterministically (highest attempt/ts/label
        # per base wins) so gates and acceptance never depend on list order.
        groups = {}
        for rec in merged:
            rec.pop("superseded_by", None)
            rec.pop("recovered", None)
            groups.setdefault(_base_label(rec.get("label")), []).append(rec)
        role_spawn = {}
        page_owners = {}
        spawn_count = dict(getattr(orch, "_spawn_count", {}) or {})
        for base, attempts in groups.items():
            active = max(attempts, key=lambda r: (
                int(r.get("attempt") or _attempt_of(r.get("label"))),
                float(r.get("ts") or 0.0),
                str(r.get("label") or ""),
            ))
            max_attempt = max(
                int(r.get("attempt") or _attempt_of(r.get("label"))) for r in attempts)
            for older in attempts:
                if older is active:
                    continue
                older["superseded_by"] = active["label"]
                older["recovered"] = bool(active.get("clean"))
            spawn_count[base] = max(spawn_count.get(base, 0), max_attempt)
            kind = str(active.get("kind") or _task_kind(active.get("label"), None))
            if kind in {"research", "review"}:
                # Budget by the highest allocated attempt per base (sparse-safe).
                role_spawn[kind] = role_spawn.get(kind, 0) + max_attempt
            if kind == "slide":
                for page in active.get("assigned_pages") or []:
                    try:
                        page_owners[int(page)] = base
                    except (TypeError, ValueError):
                        continue
        # Deterministic global ordering so consumers using "latest" are stable.
        merged.sort(key=lambda r: (float(r.get("ts") or 0.0),
                                   int(r.get("attempt") or _attempt_of(r.get("label"))),
                                   str(r.get("label") or "")))
        orch.worker_recs = merged
        orch._spawn_count = spawn_count
        # Keep the max of live and reconciled role counts (never lose a live
        # dispatch that has not yet written a terminal record).
        live_roles = dict(getattr(orch, "_role_spawn_count", {}) or {})
        for kind, count in role_spawn.items():
            live_roles[kind] = max(int(live_roles.get(kind, 0) or 0), count)
        orch._role_spawn_count = live_roles
        merged_owners = dict(getattr(orch, "_slide_page_owners", {}) or {})
        merged_owners.update(page_owners)
        orch._slide_page_owners = merged_owners


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False



_PRODUCTION_GROUP_RE = re.compile(
    r"(?mi)^\s*[-*+]\s*(?:\*\*)?production_group(?:\*\*)?\s*[:：]\s*`?([A-Za-z0-9._-]+)"
)


def _planned_production_groups(ws):
    """Return the frozen page sets from plan/slide_NN.md.

    Older/revision workspaces with no group field remain readable.  Once any
    plan declares the field, every page must declare exactly one group so the
    dispatcher cannot silently turn a multi-page group into per-page workers.
    """
    pages = []
    for path in sorted(glob.glob(os.path.join(ws, "plan", "slide_*.md"))):
        match = re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)
        if not match:
            continue
        with open(path, encoding="utf-8") as stream:
            group_matches = _PRODUCTION_GROUP_RE.findall(stream.read())
        pages.append((int(match.group(1)), sorted(set(group_matches))))
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


def _group_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _task_group_id(task, planned_groups):
    keyed = {}
    for group_id in planned_groups:
        keyed.setdefault(_group_key(group_id), []).append(group_id)
    candidates = []
    label = str(task.get("label") or "")
    for prefix in ("slide_group_", "slide-group-"):
        if label.lower().startswith(prefix):
            candidates.append(label[len(prefix):])
    goal = str(task.get("goal") or "")
    match = re.search(r"slide\s+group\s+([A-Za-z0-9._-]+)", goal, re.I)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        matches = keyed.get(_group_key(candidate), [])
        if len(matches) == 1:
            return matches[0]
    page_set = set(_slide_group_pages(task))
    exact = [group_id for group_id, pages in planned_groups.items() if page_set == set(pages)]
    return exact[0] if len(exact) == 1 else None


def _expected_slide_tasks(ws):
    try:
        groups = _planned_production_groups(ws)
    except ValueError:
        return []
    return [
        {
            "label": "slide_group_" + re.sub(r"[^A-Za-z0-9_.-]+", "-", group_id).strip("-").lower(),
            "goal": (
                f"Slide Group {group_id} "
                f"[{','.join(f'{page:02d}' for page in sorted(pages))}]: "
                "execute the frozen Production Group"
            ),
        }
        for group_id, pages in sorted(groups.items(), key=lambda item: min(item[1]))
    ]


def _canonicalize_slide_tasks(parent, tasks):
    """Resolve Slide ownership from the frozen plan, not repeated prose.

    Common legacy page expressions are accepted.  Per-page tasks that together
    cover one frozen group are deterministically coalesced into one worker so a
    weak Orchestrator cannot silently discard its own grouping decision.
    """
    if bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode")):
        return tasks, None
    try:
        planned_groups = _planned_production_groups(parent.ws)
    except ValueError as error:
        return tasks, str(error)
    if not planned_groups:
        return tasks, None
    slide_tasks = [
        task for task in tasks
        if _task_kind(task.get("label"), task.get("goal")) == "slide"
    ]
    if not slide_tasks:
        return tasks, None
    page_to_group = {
        page: group_id
        for group_id, pages in planned_groups.items()
        for page in pages
    }
    # A weak Orchestrator may dispatch one frozen group per tool call instead
    # of placing the whole deck in one delegate_task payload.  That is a valid
    # scheduling choice: ownership must be complete at final acceptance, not
    # necessarily in the first dispatch call.  Keep only groups represented in
    # this call and canonicalize each represented group to its full frozen page
    # set.
    buckets = {}
    for task in slide_tasks:
        parsed_pages = set(_slide_group_pages(task))
        group_id = _task_group_id(task, planned_groups)
        resolved_by_id = group_id is not None
        if group_id is None:
            groups = {page_to_group.get(page) for page in parsed_pages}
            if not parsed_pages or None in groups or len(groups) != 1:
                return tasks, (
                    "Slide 委派无法映射到唯一冻结 production_group；"
                    f"task={task.get('label') or 'slide'} parsed_pages={sorted(parsed_pages)}"
                )
            group_id = next(iter(groups))
        planned_pages = set(planned_groups[group_id])
        if parsed_pages and not parsed_pages.issubset(planned_pages):
            return tasks, (
                f"Slide 委派的组 ID 与页码冲突: group={group_id} "
                f"parsed={sorted(parsed_pages)} planned={sorted(planned_pages)}"
            )
        buckets.setdefault(group_id, []).append((task, parsed_pages, resolved_by_id))

    canonical = []
    for group_id, entries in sorted(buckets.items(), key=lambda item: min(planned_groups[item[0]])):
        planned_pages = set(planned_groups[group_id])
        parsed_union = set().union(*(pages for _, pages, _ in entries))
        # Even a legacy per-page request is expanded to the complete frozen
        # group.  This preserves the plan's grouping decision and prevents a
        # later worker from independently redesigning another page in the same
        # group.
        page_list = ",".join(f"{page:02d}" for page in sorted(planned_pages))
        original_goals = [str(task.get("goal") or "").strip() for task, _, _ in entries]
        original_contexts = [str(task.get("context") or "").strip() for task, _, _ in entries]
        merged_toolsets = []
        for task, _, _ in entries:
            for toolset in task.get("toolsets") or []:
                if toolset not in merged_toolsets:
                    merged_toolsets.append(toolset)
        canonical.append({
            "label": "slide_group_" + re.sub(
                r"[^A-Za-z0-9_.-]+", "-", group_id
            ).strip("-").lower(),
            "goal": (
                f"Slide Group {group_id} [{page_list}]: complete every page in this "
                "frozen Production Group.\n\nOriginal directives:\n"
                + "\n---\n".join(goal for goal in original_goals if goal)
            ),
            "context": "\n\n".join(value for value in original_contexts if value),
            "toolsets": merged_toolsets or ["file", "terminal", "vision"],
            "role": "leaf",
        })
    non_slides = [
        task for task in tasks
        if _task_kind(task.get("label"), task.get("goal")) != "slide"
    ]
    return non_slides + canonical, None


def _grounding_before_downstream_error(parent, tasks):
    """Keep evidence handoff ordered before Image/Slide/Review production.

    This is a narrow state invariant, not a presentation rule: when the run has
    attachments or has executed Material/Research, downstream workers must read
    the one canonical grounding file.  Rejecting the premature dispatch gives
    the Orchestrator an immediate, local correction instead of failing only at
    the very end of an otherwise expensive deck.
    """
    kinds = {_task_kind(task.get("label"), task.get("goal")) for task in tasks}
    downstream = kinds.intersection({"image", "slide", "review"})
    if not downstream:
        return None
    if kinds.intersection({"material", "research"}):
        return (
            "Material/Research 与下游 Image/Slide/Review 不能同批委派；"
            "先回收证据合同，写入并验证 plan/grounded-knowledge.md，再继续。"
        )
    with parent._spawn_lock:
        workers = list(parent.worker_recs)
    evidence_workers = [
        worker for worker in workers
        if str(worker.get("kind") or "").lower() in {"material", "research"}
        or str(worker.get("label") or "").lower().startswith(("material", "research"))
    ]
    has_attachments = bool(
        int((getattr(parent, "generation_preferences", {}) or {}).get("attachment_count") or 0)
    )
    revision_mode = bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode"))
    if has_attachments and not any(
            str(worker.get("kind") or "").lower() == "material"
            or str(worker.get("label") or "").lower().startswith("material")
            for worker in evidence_workers) and not revision_mode:
        return "存在用户附件，但 Material 尚未完成；不能提前委派下游制作。"
    if not evidence_workers and not has_attachments:
        return None
    grounded = os.path.join(parent.ws, "plan", "grounded-knowledge.md")
    try:
        valid = os.path.isfile(grounded) and os.path.getsize(grounded) >= 40
    except OSError:
        valid = False
    if not valid:
        return (
            "Material/Research 的合同状态仅用于诊断；当前真正缺少的是有效的 "
            "plan/grounded-knowledge.md。"
            "请立即综合用户事实、外部核验、假设与 unresolved，read_file 验证后再委派。"
        )
    return None


_IMAGE_OPPORTUNITY_LINE_RE = re.compile(
    r"(?im)^\s*[-*+]?\s*(?:\*\*)?image_opportunity(?:\*\*)?\s*[:：]\s*(.+?)\s*$"
)
_PLAN_ASSET_ID_LINE_RE = re.compile(
    r"(?im)^\s*[-*+]?\s*(?:\*\*)?asset[_ -]?id(?:\*\*)?\s*[:：=]\s*(.+?)\s*$"
)
_PLAN_PRESENTATION_RE = re.compile(
    r"(?im)^\s*[-*+]?\s*(?:\*\*)?presentation(?:\*\*)?\s*[:：]\s*`?([A-Za-z-]+)"
)
_PLAN_SUBJECT_ONLY_RE = re.compile(
    r"(?im)^\s*[-*+]?\s*(?:\*\*)?subject_only(?:\*\*)?\s*[:：]\s*true\s*$"
)
# Kept in lock-step with deck.py: a real raster asset path in the visual section
# is authoritative bitmap evidence, and a raster medium string is a legacy
# fallback when image_opportunity is absent.
_PLAN_RASTER_PATH_RE = re.compile(
    r"(?i)assets/[A-Za-z0-9_./-]+\.(?:png|jpe?g|webp|gif)"
)
_PLAN_RASTER_MEDIUM_RE = re.compile(
    r"(?im)^\s*[-*+]\s*medium\s*[:：]"
    r".*(?:photo|photograph|generated[ -]?image|bitmap|raster|生成图|照片"
    r"|(?<![无不])位图)"
)
# Visual-implementation section headings (mirror deck.py VISUAL_HEADINGS) so
# raster-path detection is scoped there and never false-matches a speech/source
# section that happens to mention a path.
_VISUAL_HEADINGS = {
    "视觉实现", "visual implementation", "visual handoff", "visual direction",
}


def _plan_visual_section(text):
    """Return the visual-implementation section body, or "" when it is absent.

    Mirrors deck.py ``_first_section(..., VISUAL_HEADINGS)``: no visual heading →
    empty string (never the whole document), so a path/presentation that only
    appears in a speech/source section is never read as visual-implementation
    content.
    """
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    for index, match in enumerate(matches):
        heading = re.sub(r"\s+", " ", match.group(1).strip().lower()).rstrip(":：")
        if heading in _VISUAL_HEADINGS:
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            return text[match.end():end]
    return ""


_IMAGE_PRESENTATIONS = {"subject-only", "framed-scene", "full-bleed", "evidence-crop"}
_NO_BITMAP_VALUES = {
    "none", "no", "code", "code_only", "code-only", "canvas",
    "canvas_only", "canvas-only", "chart", "chart_only", "chart-only",
    "typography", "typography_only", "typography-only",
}
# CJK ways plans express "no bitmap on this page".  Language-model plans often
# write the machine field in Chinese (``image_opportunity: 无位图``) instead of
# the English enum; both the startup dispatch gate and the final
# acceptance must read these as no-bitmap, exactly like ``none``.
_NO_BITMAP_CJK_RE = re.compile(
    r"^(?:无|none|无需|不需|不用)?"
    r"(?:位图|配图|图片|图像|插图|图)?"
    r"(?:需求|机会)?$"
)
_NO_BITMAP_CJK_VALUES = {
    "无", "无位图", "无需配图", "不需配图", "无需图片", "无需图像",
    "无需插图", "无图", "无需位图", "不需要配图", "不需要图片", "无配图",
    "无位图需求", "无配图需求",
}
_BITMAP_EXCEPTION_BASES = {
    "explicit_user_request", "pure_typography", "pure_chart", "wireframe",
    "accuracy_critical",
}


def _normalize_image_opportunity(raw_value):
    """Return the leading machine-readable image-opportunity enum.

    Plans are written by language models and commonly append a human-readable
    explanation with either ASCII or CJK punctuation, for example
    ``none（数据页，图表本身就是主视觉）``.  The dispatch gate must validate the
    leading enum instead of treating the entire prose suffix as part of it.
    """
    value = str(raw_value or "").strip().lower()
    match = re.match(r"([a-z][a-z0-9_-]*)", value)
    if match:
        return match.group(1)
    return re.split(r"[\s,，;；(/（]", value, maxsplit=1)[0]


def _image_opportunity_needs_bitmap(raw_value):
    """Single source of truth for whether a page declares a bitmap opportunity.

    Both the pre-Slide dispatch gate (``agent``) and the final
    acceptance (``presenter``) call this so the two never diverge.  A page
    needs a bitmap unless its declared opportunity is a no-bitmap enum (``none``,
    ``chart_only`` …) or an equivalent CJK phrase (``无位图``、``无需配图``).
    """
    raw = str(raw_value or "").strip()
    if not raw:
        return False
    normalized = _normalize_image_opportunity(raw)
    if normalized in _NO_BITMAP_VALUES:
        return False
    # CJK "none" phrasing: strip trailing human explanation after punctuation,
    # then compare the leading token against known no-bitmap phrases.
    cjk_head = re.split(r"[\s,，;；:：(/（]", raw, maxsplit=1)[0].strip()
    if cjk_head in _NO_BITMAP_CJK_VALUES:
        return False
    if _NO_BITMAP_CJK_RE.fullmatch(cjk_head) and re.search(r"[无不]", cjk_head):
        return False
    return True


def _plan_asset_ids(text):
    """Return stable IDs from one or more plan fields, including CJK syntax."""
    found = set()
    for raw_value in _PLAN_ASSET_ID_LINE_RE.findall(str(text or "")):
        for item in re.split(r"[,，]", raw_value):
            match = re.match(r"\s*[`'\"]?([A-Za-z0-9._-]+)", item)
            if match:
                found.add(match.group(1))
    return sorted(found)


def _slide_image_plan(ws):
    """Read the frozen per-page bitmap decision and stable asset references."""
    plans = []
    for path in sorted(glob.glob(os.path.join(ws, "plan", "slide_*.md"))):
        match = re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)
        if not match:
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as stream:
                text = stream.read()
        except OSError:
            continue
        # image_opportunity is a whole-doc machine field (same as deck.py, which
        # reads it from the full text); everything else about the *bitmap* — the
        # presentation contract, subject_only flag, raster asset path and raster
        # medium — is parsed ONLY from the visual-implementation section so a
        # speech/source section can never supply or satisfy the contract.
        opportunity = _IMAGE_OPPORTUNITY_LINE_RE.search(text)
        raw_value = opportunity.group(1).strip() if opportunity else ""
        normalized = _normalize_image_opportunity(raw_value)
        needs_bitmap = _image_opportunity_needs_bitmap(raw_value)
        visual = _plan_visual_section(text)
        presentation_match = _PLAN_PRESENTATION_RE.search(visual)
        presentation = (
            presentation_match.group(1).lower() if presentation_match else ""
        )
        has_raster_asset = bool(_PLAN_RASTER_PATH_RE.search(visual))
        has_raster_medium = bool(_PLAN_RASTER_MEDIUM_RE.search(visual))
        plans.append({
            "page": int(match.group(1)),
            "declared": bool(opportunity),
            "image_opportunity": normalized,
            "needs_bitmap": needs_bitmap,
            "presentation": presentation,
            "subject_only": bool(_PLAN_SUBJECT_ONLY_RE.search(visual)),
            "asset_ids": _plan_asset_ids(text),
            "has_raster_asset": has_raster_asset,
            "has_raster_medium": has_raster_medium,
        })
    return plans


def _bitmap_exception_error(ws, plans):
    """Validate the explicit, machine-readable exception for an all-no-bitmap deck."""
    path = os.path.join(ws, "plan", "image-strategy.json")
    try:
        with open(path, encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        return (
            "逐页计划没有任何位图机会，但缺少 plan/image-strategy.json 的确定性复核。"
            "请重新扫描可见人物、场地、产品、作品、活动、体验和情绪主画面；"
            "只有确属例外时才写入 bitmap_exception。"
        )
    basis = str(payload.get("exception_basis") or "").strip().lower()
    reason = str(payload.get("exception_reason") or "").strip()
    covered = {int(page) for page in payload.get("reviewed_pages") or []
               if str(page).isdigit()}
    expected = {item["page"] for item in plans}
    if (str(payload.get("status") or "").strip().lower() != "bitmap_exception"
            or payload.get("visible_subject_scan_complete") is not True
            or basis not in _BITMAP_EXCEPTION_BASES
            or len(reason) < 20
            or covered != expected):
        return (
            "plan/image-strategy.json 的无位图复核无效；必须包含 status=bitmap_exception、"
            "visible_subject_scan_complete=true、合法 exception_basis、具体理由，"
            "并用 reviewed_pages 覆盖全部页面。"
        )
    return None


def _catalog_active_by_id(entries):
    """Index catalog entries by asset_id using ACTIVE-only, order-independent rules.

    A catalog legitimately keeps history: the same ``asset_id`` may appear once as
    ``rejected``/``superseded`` (an earlier candidate) and once ``ready`` (the
    chosen asset).  Return ``{asset_id: (entry_or_None, status)}`` where status is:
      - "ok"        exactly one active (non-rejected) entry → that entry
      - "ambiguous" more than one active entry with the same id → None
      - "rejected"  only rejected/superseded entries exist → None
    Array order never changes the result.
    """
    dead = {"rejected", "superseded", "replaced"}
    grouped = {}
    for item in entries:
        if not isinstance(item, dict) or not item.get("asset_id"):
            continue
        grouped.setdefault(str(item.get("asset_id")), []).append(item)
    resolved = {}
    for asset_id, items in grouped.items():
        active = [it for it in items if str(it.get("status") or "").lower() not in dead]
        if not active:
            resolved[asset_id] = (None, "rejected")
        elif len(active) > 1:
            resolved[asset_id] = (None, "ambiguous")
        else:
            resolved[asset_id] = (active[0], "ok")
    return resolved


def _catalog_asset_error(ws, required_ids):
    """Require each frozen asset id to resolve to a ready file inside the workspace."""
    catalog_path = os.path.join(ws, "assets", "catalog.json")
    try:
        with open(catalog_path, encoding="utf-8") as stream:
            payload = json.load(stream)
        entries = payload.get("assets") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise TypeError("assets is not a list")
    except (OSError, ValueError, TypeError) as exc:
        return f"Image Agent 素材清单 assets/catalog.json 无法验收: {type(exc).__name__}"
    resolved = _catalog_active_by_id(entries)
    missing, invalid, ambiguous = [], [], []
    root = os.path.abspath(ws)
    for asset_id in sorted(required_ids):
        item, state = resolved.get(asset_id, (None, "missing"))
        if state == "ambiguous":
            ambiguous.append(asset_id)
            continue
        if item is None:
            missing.append(asset_id)
            continue
        raw_path = str(item.get("path") or "").strip()
        actual = raw_path if os.path.isabs(raw_path) else os.path.join(root, raw_path)
        actual = os.path.abspath(actual)
        try:
            inside = os.path.commonpath([root, actual]) == root
        except ValueError:
            inside = False
        if str(item.get("status") or "").lower() != "ready" or not inside \
                or not os.path.isfile(actual):
            invalid.append(asset_id)
    if missing or invalid or ambiguous:
        return (
            "Image Agent 素材清单未闭环: "
            f"missing={missing[:8]} not_ready={invalid[:8]} ambiguous={ambiguous[:8]}"
        )
    return None


def _disk_image_stage_ready(ws):
    """Persisted-truth fallback for the Image→Slide gate.

    Trust only real ``handoff.json`` completion (never a ledger/start/failure
    ``clean``).  Clears the gate only when there is at least one active Image base
    AND *every* active Image base is clean+ready — a single active blocked base,
    or a ledger-only clean with no handoff, must not clear it.
    """
    active = _active_workers_from_disk(ws, "image", clean_source_only=True)
    if not active:
        return False
    return all(
        bool(rec.get("clean"))
        and str((rec.get("contract") or {}).get("status") or "").lower() == "ready"
        for rec in active
    )


def _plan_image_contract_error(ws):
    """Deterministic per-page image-contract gate, run BEFORE Image/Slide dispatch.

    Aligns with deck.py prepare/build so the run fails here — not at the final
    build after the whole pipeline ran.  A page is a raster page (and must carry
    a valid four-enum ``presentation``, plus ``subject_only: true`` for
    ``subject-only``) when EITHER:
      - its ``image_opportunity`` machine enum needs a bitmap, OR
      - the visual section already carries a real raster asset path
        (``assets/foo.png|jpg|jpeg|webp|gif``) — authoritative bitmap evidence,
        even if the declared opportunity says ``none``, OR
      - ``image_opportunity`` is entirely absent but the visual section declares a
        raster medium (skipped-prepare / legacy plans).
    A genuinely no-bitmap page (no-bitmap enum, no raster path) is not validated,
    so ``无位图 (chart+timeline)`` is never a false positive.
    """
    plans = _slide_image_plan(ws)
    bad = []
    undeclared = [item["page"] for item in plans if not item.get("declared")]
    if undeclared:
        bad.append(
            "缺少机器字段 image_opportunity: pages="
            + str(undeclared[:12])
            + "（必须写成单行枚举，不能写成空的 image_opportunity: 块）"
        )
    for item in plans:
        is_raster = (
            item.get("needs_bitmap")
            or item.get("has_raster_asset")
            or (not item.get("declared") and item.get("has_raster_medium"))
        )
        if not is_raster:
            continue
        presentation = item.get("presentation") or ""
        if presentation not in _IMAGE_PRESENTATIONS:
            reason = (
                "presentation=" + (presentation or "<缺失>") + " 非法(需 "
                "subject-only|framed-scene|full-bleed|evidence-crop)"
            )
            if item.get("has_raster_asset") and not item.get("needs_bitmap"):
                reason += "；该页已含真实位图路径，即使 image_opportunity=none 也须声明四枚举"
            bad.append(f"page {item['page']:02d}: {reason}")
            continue
        if presentation == "subject-only" and not item.get("subject_only"):
            bad.append(f"page {item['page']:02d}: subject-only 需 subject_only: true")
    if bad:
        return (
            "逐页位图展示合同不完整，必须先在 plan/slide_NN.md 修正后再委派 "
            "Image/Slide：" + "；".join(bad[:12])
        )
    return None


def _image_before_slide_error(parent, tasks):
    """Enforce Image → manifest → Slide ordering at dispatch time."""
    kinds = {_task_kind(task.get("label"), task.get("goal")) for task in tasks}
    if "slide" not in kinds:
        return None
    if "image" in kinds:
        return (
            "Image 与 Slide 不能同批委派；先等待 Image Agent 完成并验收 "
            "assets/catalog.json，再启动 Slide Agent。"
        )
    plans = _slide_image_plan(parent.ws)
    if not plans:
        return "Slide 启动前缺少 plan/slide_NN.md，无法冻结逐页配图机会。"
    undeclared = [item["page"] for item in plans if not item["declared"]]
    if undeclared:
        return f"逐页计划缺少 image_opportunity: pages={undeclared[:12]}"
    image_pages = [item for item in plans if item["needs_bitmap"]]
    if not image_pages:
        return _bitmap_exception_error(parent.ws, plans)
    missing_ids = [item["page"] for item in image_pages if not item["asset_ids"]]
    if missing_ids:
        return (
            "存在配图机会，但逐页计划尚未回填稳定 asset_id: "
            f"pages={missing_ids[:12]}"
        )
    required_ids = {asset_id for item in image_pages for asset_id in item["asset_ids"]}
    revision_mode = bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode"))
    if not revision_mode:
        with parent._spawn_lock:
            recs = list(parent.worker_recs)
        # Only the EFFECTIVE ACTIVE attempt per base counts: a stale superseded
        # ``ready`` must never mask the current active ``blocked`` attempt, and
        # the result must not depend on list order.
        image_workers = _effective_active_recs(recs, kind="image")
        blocked_active = [
            w for w in image_workers
            if not (w.get("clean") and str(
                (w.get("contract") or {}).get("status") or "").lower() == "ready")
        ]
        ready_active = [w for w in image_workers if w not in blocked_active]
        # In-memory worker_recs freeze the clean flag at completion time.  A later
        # correction (a re-run image-finalize, or a repaired handoff.json) must be
        # recoverable from the persisted handoff truth.  But a live active blocked
        # attempt still blocks unless disk shows every image base clean+ready.
        # A live active blocked Image attempt ALWAYS blocks Slide dispatch: an
        # unrelated disk handoff (a different base) must never cross-heal it.
        # Only a same-base higher-attempt handoff recovers it, and normal
        # delegate_task has already reconciled disk truth into worker_recs before
        # this gate, so a genuine same-base recovery is reflected in memory here.
        if blocked_active:
            labels = [str(w.get("label")) for w in blocked_active]
            return (
                "存在未完成的 active Image 分片(status!=ready)，"
                f"不得启动 Slide: {labels[:8]}"
            )
        # No active blocked base, but also nothing ready in memory (e.g. memory has
        # no Image record yet on a resumed run).  Fall back to persisted handoff
        # truth: only clears when at least one image base finished and every active
        # image base is clean+ready (real handoff, never ledger-only).
        if not ready_active and not _disk_image_stage_ready(parent.ws):
            return (
                "计划存在配图机会，但尚无完成且 status=ready 的 Image Agent；"
                "禁止 Slide Agent 绕过素材阶段直接制作或自行生图。"
            )
    return _catalog_asset_error(parent.ws, required_ids)


def _reserve_singleton_roles(parent, tasks):
    """Reserve singleton roles plus bounded Review/Slide repair attempts."""
    requested = {}
    slide_owners = {}
    for task in tasks:
        kind = _task_kind(task.get("label"), task.get("goal"))
        if kind in {"research", "review"}:
            requested[kind] = requested.get(kind, 0) + 1
        if kind == "slide":
            label = str(task.get("label") or "slide")
            pages = _slide_group_pages(task)
            if not pages:
                return f"{label} 未声明负责页码；Slide Group 必须显式列出唯一 pages"
            for page in pages:
                if page in slide_owners:
                    return (
                        f"Slide 页码 {page:02d} 在同一批次重复归属："
                        f"{slide_owners[page]} 与 {label}"
                    )
                slide_owners[page] = label
    duplicate = next((kind for kind, count in requested.items() if count > 1), None)
    if duplicate:
        return f"{duplicate} 是任务级单例；同一批次不能创建多个实例"
    with parent._spawn_lock:
        for kind in requested:
            used = int(parent._role_spawn_count.get(kind, 0) or 0)
            if kind == "review":
                limit = MAX_REVIEW_ATTEMPTS
            elif kind == "research":
                limit = MAX_RESEARCH_ATTEMPTS
            else:
                limit = 1
            if used >= limit:
                return (
                    f"{kind} 已达到受控执行上限 {limit}；停止继续重派，"
                    "保留现有问题账本并进入可交付收尾"
                )
            if kind in {"review", "research"} and used:
                peers = _effective_active_recs(
                    getattr(parent, "worker_recs", []) or [], kind=kind)
                latest = peers[-1] if peers else {}
                latest_status = str((latest.get("contract") or {}).get("status") or "").lower()
                # A succeeded prior attempt blocks a redundant second active instance.
                if kind == "review" and latest.get("clean") and latest_status == "ready":
                    return "Review 已返回 ready；不得为审美偏好重复复验"
                if kind == "research" and latest.get("clean") and latest_status in {"ready", "partial"}:
                    return "Research 已完成；任务级单例不得重复委派"
                # A repeat dispatch is a recovery, allowed ONLY after the prior
                # attempt has a TERMINAL failed record (timeout/crash/blocked/
                # clean=False).  No terminal record means the prior attempt is
                # still in-flight — refuse, or we would run two concurrently.
                terminal_failed = bool(peers) and not latest.get("clean")
                if not terminal_failed:
                    return (
                        f"{kind} 仍在进行中(无 terminal 失败记录)；"
                        "任务级单例不得并发第二个实例"
                    )
        existing_owners = getattr(parent, "_slide_page_owners", {})
        all_recs = getattr(parent, "worker_recs", []) or []
        # Deterministic effective-active slide attempt per base (order-independent).
        latest_slide_attempts = {
            _base_label(rec.get("label")): rec
            for rec in _effective_active_recs(all_recs, kind="slide")
        }

        def _slide_repair_attempts(base_label):
            # Sparse-safe: use the HIGHEST attempt allocated for this base, not the
            # count of records (a missing intermediate _rN would under-count).
            return max(
                (int(r.get("attempt") or _attempt_of(r.get("label")))
                 for r in all_recs if _base_label(r.get("label")) == base_label),
                default=0,
            )

        resumed = {}
        for page, label in slide_owners.items():
            if page in existing_owners:
                owner = str(existing_owners[page])
                previous = latest_slide_attempts.get(label)
                # A failed worker may be continued only by the exact same
                # canonical Production Group owner.  This keeps page ownership
                # stable while giving an incomplete group a recoverable path;
                # a different label can never overwrite the page.
                if owner == label and previous and not previous.get("clean"):
                    resumed[label] = str(previous.get("label") or label)
                    continue
                if owner == label and previous and previous.get("clean"):
                    reviews = _effective_active_recs(all_recs, kind="review")
                    latest_review = reviews[-1] if reviews else {}
                    review_status = str(
                        (latest_review.get("contract") or {}).get("status") or ""
                    ).lower()
                    attempts = _slide_repair_attempts(label)
                    if (latest_review and review_status == "blocked"
                            and attempts <= MAX_SLIDE_REPAIR_ATTEMPTS):
                        resumed[label] = str(previous.get("label") or label)
                        continue
                    return f"Slide 页码 {page:02d} 的 owner {label} 已完成；没有待修硬伤"
                return (
                    f"Slide 页码 {page:02d} 已归属 {owner}；"
                    f"不得再交给 {label} 并发覆盖"
                )
        parent_cfg = getattr(parent, "cfg", {}) or {}
        if slide_owners and not bool(parent_cfg.get("_revision_mode")):
            planned = {
                int(match.group(1))
                for path in glob.glob(os.path.join(parent.ws, "plan", "slide_*.md"))
                for match in [re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)]
                if match
            }
            if planned and planned != set(range(1, max(planned) + 1)):
                return (
                    "逐页计划页码必须从 01 连续到末页；"
                    f"actual={sorted(planned)[:30]}"
                )
            try:
                planned_groups = _planned_production_groups(parent.ws)
            except ValueError as error:
                return str(error)
            if planned_groups:
                expected_sets = {
                    frozenset(pages): group_id
                    for group_id, pages in planned_groups.items()
                }
                for task in tasks:
                    if _task_kind(task.get("label"), task.get("goal")) != "slide":
                        continue
                    pages = frozenset(_slide_group_pages(task))
                    if pages not in expected_sets:
                        return (
                            "Slide 委派必须与已冻结 production_group 逐组一致；"
                            f"task={task.get('label') or 'slide'} pages={sorted(pages)} "
                            f"planned={{{', '.join(f'{key}:{sorted(value)}' for key, value in planned_groups.items())}}}"
                        )
        for kind, count in requested.items():
            parent._role_spawn_count[kind] = int(parent._role_spawn_count.get(kind, 0) or 0) + count
        for task in tasks:
            label = str(task.get("label") or "")
            if label in resumed:
                task["_resume_of"] = resumed[label]
        existing_owners.update(slide_owners)
        parent._slide_page_owners = existing_owners
    return None


def _child_contract_error(parent, child, kind, contract):
    """Return the machine-readable contract error for one completed worker."""
    status = str(contract.get("status") or "").strip().lower()
    # blocked is a valid, explicit diagnostic result for visual workers.  It is
    # recorded as clean=False by _run_child, but must retain its real reason.
    if kind in {"review", "slide"} and status == "blocked":
        return ""
    if kind in {"material", "image", "review", "slide"} and status != "ready":
        return f"{kind} 必须返回 status: ready，得到 {status or 'missing'}"
    if kind == "research":
        # Research is accepted by its durable artifact, not by optional prose
        # contract fields.  A model may omit ``status``/``unresolved`` while
        # still writing a complete brief; making that omission fatal used to
        # lock every downstream Image/Slide/Review dispatch (case 422).
        declared = str(contract.get("output") or "").strip()
        candidates = [declared, "research/research.md"]
        output_path = ""
        for output in dict.fromkeys(value for value in candidates if value):
            candidate = os.path.abspath(os.path.join(parent.ws, output))
            try:
                inside = os.path.commonpath([os.path.abspath(parent.ws), candidate]) \
                    == os.path.abspath(parent.ws)
                usable = inside and os.path.isfile(candidate) and os.path.getsize(candidate) > 0
            except (OSError, ValueError):
                usable = False
            if usable:
                output_path = candidate
                break
        if not output_path:
            return f"Research 正式产物不存在或为空: {declared or 'research/research.md'}"

        contract_warnings = []
        if status not in {"ready", "partial"}:
            contract_warnings.append(f"status={status or 'missing'}")
        unresolved = str(contract.get("unresolved") or "").strip().lower()
        if status == "partial" and unresolved in {"", "none", "n/a", "not-applicable"}:
            contract_warnings.append("partial 未声明 unresolved")
        if contract_warnings:
            contract["validation_warning"] = (
                "Research 合同字段不完整，已按正式产物继续: "
                + "; ".join(contract_warnings)
            )
    return ""


def _blocking_review_failure(parent):
    """Return one bounded recovery instruction after the latest blocked Review."""
    if str(getattr(parent, "role", "") or "").lower() != "orchestrator":
        return None
    lock = getattr(parent, "_spawn_lock", None)
    if lock is None:
        records = list(getattr(parent, "worker_recs", []) or [])
    else:
        with lock:
            records = list(getattr(parent, "worker_recs", []) or [])
    reviews = _effective_active_recs(records, kind="review")
    if not reviews:
        return None
    review = reviews[-1]
    notified = str(getattr(parent, "_review_recovery_notified", "") or "")
    review_label = str(review.get("label") or "review")
    if notified == review_label:
        return None
    contract = review.get("contract") or {}
    status = str(contract.get("status") or "missing").strip().lower()
    if review.get("clean") and status == "ready":
        return None
    detail = str(
        contract.get("remaining")
        or contract.get("validation_error")
        or contract.get("summary")
        or review.get("exit_reason")
        or "Review 未通过最终质量门"
    ).strip()
    parent._review_recovery_notified = review_label
    # Budget by the highest allocated attempt / persistent role count, NOT by the
    # collapsed active count (which is 1 per base).  A review_r2 whose base has
    # already exhausted the budget must not be told to re-delegate.
    review_base = _base_label(review_label)
    max_attempt = max(
        (int(r.get("attempt") or _attempt_of(r.get("label")))
         for r in records
         if _base_label(r.get("label")) == review_base
         and (str(r.get("kind") or "").lower() == "review"
              or str(r.get("label") or "").lower().startswith("review"))),
        default=_attempt_of(review_label),
    )
    role_used = int((getattr(parent, "_role_spawn_count", {}) or {}).get("review", 0) or 0)
    attempts = max(max_attempt, role_used)
    if attempts >= MAX_REVIEW_ATTEMPTS:
        instruction = (
            "Review 复验预算已用完。不要继续改页或探测环境；保留 _trace/review-issues.md，"
            "构建 present.html 并自然收尾。只要成稿可渲染、可播放，系统会以“完成（有待改进）”交付。"
        )
    else:
        instruction = (
            "Review 发现未解决问题。只把有新鲜像素/DOM 证据的真实硬伤交回原页面 owner，"
            f"每个 Slide Group 最多重派 {MAX_SLIDE_REPAIR_ATTEMPTS} 次；修复后再委派 Review 复验。"
            "advisory 不得触发返工。若无法稳定改善，保留最佳版本与问题账本并构建交付物。"
        )
    return {
        "label": review_label,
        "status": status,
        "detail": detail,
        "instruction": instruction,
    }


def _run_child(parent, task, ticket):
    """构造 + 跑完一个子 agent,把小结挂到 parent,返回紧凑结果(不让子轨迹/图像穿透到 parent)。
    `ticket` 是父子共享小状态:父超时放弃时置 abandoned,迟到线程结束后不再写 worker_recs。"""
    child, name = _build_child(parent, task)
    ticket["label"] = name
    with parent._child_sem:                  # 父级并发闸:跨多个并发的 delegate_task 调用统一限并发
        fin = child.run()
    _restore_vision_evidence(child)
    contract = _final_contract(child.final_text)
    kind = _task_kind(name, task.get("goal"))
    if kind == "review" and not contract:
        contract = _review_ledger_contract(child)
        if contract:
            child.final_text = _contract_text(contract)
            child.exit_reason = "review_ledger_ready"
            fin = True
    if str(contract.get("status") or "").strip().lower() == "blocked":
        fin = False
    nova_precheck = getattr(child, "nova_precheck", None)
    if nova_precheck is not None and not nova_precheck.get("ok", False):
        fin = False
        contract["validation_error"] = (
            "Nova exact-raw precheck failed: "
            + "; ".join(str(item) for item in nova_precheck.get("errors", [])[:5])
        )
    transparency_error = _image_transparency_error(
        parent.ws, task, name, contract, child.final_text, child.vision_paths
    )
    if transparency_error:
        fin = False
        contract["validation_error"] = transparency_error
    assigned_pages = _slide_group_pages(task) if kind == "slide" else []
    pixel_state = _slide_pixel_state(child, assigned_pages) if kind == "slide" else {
        "inspected_pages": [], "missing_pages": [], "stale_pages": [],
        "dirty_sources": [],
    }
    inspected_pages = pixel_state["inspected_pages"]
    missing_pixel_pages = pixel_state["missing_pages"]
    stale_pixel_pages = pixel_state["stale_pages"]
    if missing_pixel_pages:
        fin = False
        contract["validation_error"] = (
            "Slide Group 缺少逐页最终像素自检: "
            + ",".join(f"{page:02d}" for page in missing_pixel_pages)
        )
    if stale_pixel_pages:
        fin = False
        contract["validation_error"] = (
            "Slide Group 最终像素证据已过期: "
            + ",".join(f"{page:02d}" for page in stale_pixel_pages)
        )
    dirty_sources = pixel_state["dirty_sources"]
    if dirty_sources:
        fin = False
        contract["validation_error"] = (
            "视觉源文件最后修改后尚未完成对应重渲: " + ",".join(dirty_sources[:8])
        )
    contract_error = _child_contract_error(parent, child, kind, contract)
    if contract_error:
        fin = False
        contract["validation_error"] = contract_error
    if kind == "slide" and not fin:
        restored = tools.restore_verified_slides(child)
        if restored:
            contract["rollback"] = "restored_last_verified_baseline"
            contract["rollback_pages"] = restored
    rec = {"label": name, "kind": kind, "clean": fin, "renders": child.n_renders,
           "vision_calls": child.n_vision_calls,
           "vision_paths": list(child.vision_paths),
           "vision_evidence": dict(getattr(child, "vision_evidence", {}) or {}),
           "trace_dir": os.path.relpath(child.trace.sub_dir, parent.ws).replace(os.sep, "/"),
           "assigned_pages": assigned_pages,
           "inspected_pages": inspected_pages,
           "stale_pixel_pages": stale_pixel_pages,
           "dirty_visual_sources": dirty_sources,
           "machine_refine_rounds": int(getattr(child, "_review_refine_rounds", 0) or 0),
           "shot": child.last_shot, "exit_reason": child.exit_reason,
           "nova_raw_precheck": nova_precheck,
           "contract": contract}
    resume_of = str(task.get("_resume_of") or "").strip()
    if resume_of:
        rec["resume_of"] = resume_of
    with parent._spawn_lock:
        if not ticket.get("abandoned"):
            if resume_of:
                for previous in reversed(parent.worker_recs):
                    if str(previous.get("label") or "") == resume_of:
                        previous["superseded_by"] = name
                        previous["recovered"] = bool(fin)
                        break
            parent.worker_recs.append(rec)
            ticket["recorded"] = True
    if transparency_error:
        contract["validation_error"] = transparency_error
    return _child_handoff(parent, child, name, fin, contract, accept_fields=rec)


def _record_worker_failure(parent, task, ticket, exit_reason, detail):
    """Persist every crashed/timed-out child as a first-class acceptance record."""
    with parent._spawn_lock:
        if ticket.get("recorded"):
            return next(
                (item for item in reversed(parent.worker_recs)
                 if item.get("label") == ticket.get("label")),
                {"contract": {"status": "blocked", "validation_error": str(detail)}},
            )
    label = str(ticket.get("label") or task.get("label") or "child")
    kind = _task_kind(label, task.get("goal"))
    assigned_pages = _slide_group_pages(task) if kind == "slide" else []
    message = str(detail or exit_reason).strip()[:1600]
    contract = {"status": "blocked", "validation_error": message}
    rec = {
        "label": label,
        "kind": kind,
        "clean": False,
        "renders": 0,
        "vision_calls": 0,
        "vision_paths": [],
        "vision_evidence": {},
        "assigned_pages": assigned_pages,
        "inspected_pages": [],
        "stale_pixel_pages": assigned_pages,
        "dirty_visual_sources": [],
        "machine_refine_rounds": 0,
        "shot": None,
        "exit_reason": exit_reason,
        "contract": contract,
    }
    trace_root = os.path.dirname(parent.trace.sub_dir)
    failure_dir = os.path.join(trace_root, "worker-failures")
    os.makedirs(failure_dir, exist_ok=True)
    failure_path = os.path.join(
        failure_dir, re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".json"
    )
    temporary = failure_path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    _failure_ts = time.time()
    payload = {
        "label": label,
        "clean": False,
        "exit_reason": exit_reason,
        "contract": contract,
        "artifacts": [],
        "final_response": "",
        # Self-describing fields for restart rebuild (mirror handoff payload).
        "kind": kind,
        "assigned_pages": assigned_pages,
        "attempt": _attempt_of(label),
        "base_label": re.sub(r"_r\d+$", "", label),
        "ts": _failure_ts,
    }
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, failure_path)
    _append_worker_ledger(parent.ws, {
        "ts": _failure_ts,
        "label": label,
        "base": re.sub(r"_r\d+$", "", label),
        "kind": kind,
        "attempt": _attempt_of(label),
        "clean": False,
        "contract_status": "blocked",
        "assigned_pages": assigned_pages,
        "handoff": "",
        "abandoned": True,
    })
    with parent._spawn_lock:
        if not ticket.get("recorded"):
            ticket["abandoned"] = True
            ticket["recorded"] = True
            parent.worker_recs.append(rec)
    return rec


def delegate_task(parent, goal=None, context=None, toolsets=None, role=None,
                  label=None, assigned_pages=None, tasks=None, **_extra):
    """并行起一批子 agent 跑任务,返回 `{"results":[...]}` 的 JSON 字符串。

    两种形态:顶层单个 `{goal,context?,toolsets?,role?,label?}`,或 `tasks` 数组批量。每次调用用一个
    **本地** ThreadPoolExecutor(用完即关,不留全局池)。单个子 agent 有硬超时:超时记一条 clean=False
    (让验收拒收)+ 标记 abandoned(迟到线程丢弃自己的记录)。"""
    if isinstance(tasks, str):        # 健壮化:某些模型把 tasks 数组二次编码成 JSON 字符串
        try:
            tasks = json.loads(tasks)
        except Exception:
            tasks = None
    if isinstance(tasks, dict):       # 单个任务被当对象(而非单元素数组)传进来
        tasks = [tasks]
    if tasks is None:
        if goal or label or toolsets:
            tasks = [{
                "goal": goal,
                "context": context,
                "toolsets": toolsets,
                "role": role,
                "label": label,
                "assigned_pages": assigned_pages,
            }]
        else:
            tasks = []
    if not isinstance(tasks, list) or not tasks:
        return json.dumps({"error": "delegate_task 需要 goal 或非空 tasks[]"}, ensure_ascii=False)
    if parent._delegate_depth >= MAX_SPAWN_DEPTH:
        return json.dumps({"error": "已到委派深度上限(叶子子 agent 不能再委派)"}, ensure_ascii=False)

    norm = [_normalize_task(t) for t in tasks]
    # Fold durable disk truth into memory before the dispatch gates so a late
    # completion / repaired handoff / interrupted attempt is seen, not just at
    # the first run_job hydrate.
    _reconcile_worker_recs(parent)
    norm, canonical_error = _canonicalize_slide_tasks(parent, norm)
    if canonical_error:
        return json.dumps({
            "error": canonical_error,
            "code": "invalid_slide_assignment",
            "expected_slide_tasks": _expected_slide_tasks(parent.ws),
            "retry": "使用 expected_slide_tasks 中的 label 和 goal 重试，不要再用叙述性页码。",
        }, ensure_ascii=False)
    grounding_error = _grounding_before_downstream_error(parent, norm)
    if grounding_error:
        return json.dumps({
            "error": grounding_error,
            "code": "grounding_required",
            "retry": "先写入并 read_file 验证 plan/grounded-knowledge.md，再重试原委派。",
        }, ensure_ascii=False)
    # Deterministic plan image-contract gate: before ANY downstream production
    # (image/slide/review), reject an illegal per-page presentation so the run
    # fails here — not at the final deck.py build after the whole pipeline ran.
    downstream_kinds = {
        _task_kind(task.get("label"), task.get("goal")) for task in norm
    }
    if downstream_kinds & {"image", "slide", "review"}:
        plan_contract_error = _plan_image_contract_error(parent.ws)
        if plan_contract_error:
            return json.dumps({
                "error": plan_contract_error,
                "code": "image_presentation_contract",
                "retry": (
                    "在每个 plan/slide_NN.md 的唯一 `## 视觉实现` 中使用同级独立行："
                    "`- image_opportunity: real_required|generated_ok|none|chart_only|"
                    "canvas_only|typography_only`；有位图时另写 "
                    "`- presentation: subject-only|framed-scene|full-bleed|evidence-crop`，"
                    "无位图时省略 presentation。full-bleed/framed-scene 不是 "
                    "image_opportunity，split-media/right-half/cards 也不是 presentation。"
                    "直接修正计划后重试；不要搜索或修改 Skill/Harness 运行时代码。"
                ),
            }, ensure_ascii=False)
    image_error = _image_before_slide_error(parent, norm)
    if image_error:
        return json.dumps({
            "error": image_error,
            "code": "image_stage_required",
            "retry": (
                "先完成逐页可见主体扫描；有配图机会时先单独委派 Image Agent，"
                "验收 assets/catalog.json 并把 ready asset_id 回填计划，再重试 Slide 委派。"
                "asset_id 行可使用半角或全角冒号；若素材已 ready，直接按 error 中的 pages "
                "修正计划并重试，不要搜索 Skill/Harness 运行时代码或全盘搜索错误字符串。"
            ),
        }, ensure_ascii=False)
    singleton_error = _reserve_singleton_roles(parent, norm)
    if singleton_error:
        payload = {"error": singleton_error}
        if "Slide" in singleton_error or "production_group" in singleton_error:
            payload.update({
                "code": "invalid_slide_assignment",
                "expected_slide_tasks": _expected_slide_tasks(parent.ws),
                "retry": "使用 expected_slide_tasks 中的 label 和 goal 重试。",
            })
        return json.dumps(payload, ensure_ascii=False)

    def run_one(nt, ticket):
        try:
            return _run_child(parent, nt, ticket)
        except Exception as e:
            rec = _record_worker_failure(
                parent, nt, ticket, "crashed", f"{type(e).__name__}: {e}"
            )
            return {"status": "error",
                    "exit_reason": "crashed", "contract": rec["contract"],
                    "renders": 0, "shot": None, "summary": f"子 agent 崩溃: {e}"}

    ex = cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN, thread_name_prefix="child")
    try:
        triples = []
        for nt in norm:
            ticket = {"abandoned": False, "label": nt.get("label")}
            triples.append((nt, ticket, ex.submit(run_one, nt, ticket)))
        out = []
        for nt, ticket, f in triples:
            _ts = nt.get("toolsets") or []
            _lbl = (nt.get("label") or "")
            if "image_gen" in _ts:
                to = IMAGE_WORKER_TIMEOUT
            elif _lbl.startswith("material"):   # material 自己跑解析脚本 + 大 deck 忠实抄录,按页数放大
                to = min(MATERIAL_WORKER_TIMEOUT + MATERIAL_WORKER_TIMEOUT_PER_PAGE * _deck_n_slides(parent),
                         MATERIAL_WORKER_TIMEOUT_CAP)
            elif any(k in _lbl for k in ("review", "audience", "listener", "audit", "gate")):
                # review/audience/audit/gate 都要读**整册**逐页核对(designer_audit/presenter_audit/designer_gate…),
                # 超时与页数强相关 → base + k×页数。用子串匹配:designer_audit 不 startswith "audit",
                # 旧 startswith 漏判 → 掉 600 默认档被误杀(2026-07-16 修:designer_audit 11 + designer_gate 6 超时)
                to = min(REVIEW_WORKER_TIMEOUT_BASE + REVIEW_WORKER_TIMEOUT_PER_PAGE * _deck_n_slides(parent),
                         REVIEW_WORKER_TIMEOUT_CAP)
            elif _lbl.startswith("slide"):
                to = _slide_timeout_budget(nt)
            else:
                to = WORKER_TIMEOUT
            try:
                out.append(f.result(timeout=to))
            except cf.TimeoutError:
                lbl = ticket.get("label") or "child"
                parent.log(f"子 agent {lbl} 超过 {to}s,放弃")
                rec = _record_worker_failure(
                    parent, nt, ticket, "timeout", f"worker exceeded {to}s"
                )
                out.append({"status": "timeout", "renders": 0,
                            "shot": None, "contract": rec["contract"],
                            "summary": f"子 agent 超过 {to}s 被放弃"})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return json.dumps({"results": out}, ensure_ascii=False)
