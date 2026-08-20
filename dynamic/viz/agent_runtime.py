"""服务端 agentic 执行循环 —— 纯 OpenAI 接口端到端。

驱动 dazzle-v2 学生模型（swift deploy / vLLM，OpenAI 兼容端点）多轮把 deck 做出来。
标准 OpenAI 流程：把 messages(role/content/tool_calls/tool) + tools=[...] 发给端点，
端点用模型 chat template 渲染 + 内置 tool-call 解析器回**结构化 tool_calls**；本模块只负责
工具执行 + 事件流。messages.json 即标准 OpenAI 轨迹（可读、可复用），不再有手拼 XML。

训推一致说明：训练数据的工具定义块是 XML(ms-swift agent_data_tools)，端点用 tools= 渲染成 JSON；
两者工具名/参数/输出格式 `<tool_call><function=…>` 一致，模型据 SKILL.md 与工具名行事，
定义块括号语法差异不影响行为（已实测：首步仍精确 read_file SKILL.md）。thinking 由端点用
--enable_thinking false 注入空 `<think></think>`，与训练 add_non_thinking_prefix 对齐。

运行：viz_server 在 fancy-sft conda env 下启动（playwright/PIL/requests；render_deck.py 的
ensure_browser_libs 靠 CONDA_PREFIX 自愈 chromium）。每会话独立 workspace + events.jsonl。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent          # viz/
ROOT = HERE.parent                                # 仓库根
sys.path.insert(0, str(ROOT))

# ★加载仓库根 .env：web 服务（launch_web.sh）不像 batch stage 脚本那样自带 load_dotenv，
#   且 launch_web.sh 不 export OPENAI_API_KEY → 进程环境常缺生图 key，image_generate 发出空
#   Bearer 被网关判 "Invalid token"。在导入 config(模块级读 OPENAI_BASE_URL/IMAGE_MODEL) 前补上。
#   缺 dotenv 时静默跳过：key 若已 export 仍可用，绝不因此拖垮服务。
try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from agentic import config as acfg  # noqa: E402  路径/画布常量
from agentic import tools as atools  # noqa: E402  与训练同款工具实现

SKILLS_DIR = str(acfg.SKILLS_DIR)
STUDIO_DIR = HERE / "studio_runs"
SYSTEM_FILE = HERE / "serve" / "dazzle_system_prompt.txt"

HARNESS_SUFFIX = "（请先 read_file skills/dazzle-deck/SKILL.md，按其流程在当前工作区完成，最终产物为 deck.html。）"
HARNESS_SUFFIX_EN = ("(First read_file skills/dazzle-deck/SKILL.md, then follow its workflow in the current "
                     "workspace; the final artifact is deck.html.)")


def _harness_suffix(language: str) -> str:
    """首条 user 消息拼的 harness 提示——按 query 语言选中/英，避免给英文 query 塞中文指令
    污染语言上下文（与 SKILL.md『语言跟 query 走、不做中英并行』契约一致）。"""
    return HARNESS_SUFFIX_EN if str(language or "").lower() == "en" else HARNESS_SUFFIX


# 上下文水位：训练 max_length=131072(128K)。
CTX_LIMIT = int(os.environ.get("STUDIO_CTX_LIMIT", "120000"))
CTX_HEADROOM = 4000
PER_TURN_MAX_TOKENS = int(os.environ.get("STUDIO_MAX_TOKENS", "40960"))
MAX_TURNS = int(os.environ.get("STUDIO_MAX_TURNS", "130"))
TOOL_RESULT_CAP = 8000
IMAGE_TOKENS = 1024   # 对齐 IMAGE_MAX_TOKEN_NUM（上下文估算用）
# 学生模型常"光说不做"（输出一轮叙述意图却没调工具）。teacher 不会，但 9B 会 → 不能一见
# 无 tool_call 就收尾。deck 还没真渲染出来时，提示它继续推进，最多 MAX_HEALS 次。
MAX_HEALS = int(os.environ.get("STUDIO_MAX_HEALS", "4"))
NUDGE_PROMPT = ("[系统] 请继续按 SKILL.md 流程推进：用工具实际产出/修改文件（write_file/edit/bash 渲染/"
                "vision_analyze 自检），不要只用文字描述你打算做什么。deck.html 完成并自检通过后，"
                "再用一段文字收尾。")

# 采样参数（环境变量可调）。temperature 默认 0.3：9B 学生偏弱，低温更确定、edit 更易精确匹配；
# ★绝不要 0（贪心会复读死循环撞 max_tokens）。top_p/top_k/rep 沿用 Qwen WebDev 预设。
TEMPERATURE = float(os.environ.get("STUDIO_TEMPERATURE", "0.3"))
TOP_P = float(os.environ.get("STUDIO_TOP_P", "0.95"))
TOP_K = int(os.environ.get("STUDIO_TOP_K", "20"))
REPETITION_PENALTY = float(os.environ.get("STUDIO_REP_PENALTY", "1.05"))

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


# ─────────────────────────────────────────────── system prompt + 工具(OpenAI 格式)
def _load_base_system() -> str:
    """取训练同款 BASE_SYSTEM（dazzle_system_prompt.txt 里 </IMPORTANT> 之后那段自然语言）。
    工具定义不放在 system，改由 tools= 传给端点（端点模板渲染）。"""
    full = SYSTEM_FILE.read_text(encoding="utf-8")
    base = full.split("</IMPORTANT>", 1)[-1].strip() if "</IMPORTANT>" in full else full.strip()
    if not base.startswith("你是一位顶级前端工程师"):
        raise RuntimeError(f"BASE_SYSTEM 自检失败：开头不符（{SYSTEM_FILE}）")
    return base


# studio 在线体验专用的 bash 说明（让模型知道现在可用 sed/awk/mv/cp/rm 等）。仅覆盖发给端点的
# 工具描述，不动 tools.py 里 teacher 造数据用的 schema 字符串，训练分布不变。
_STUDIO_BASH_DESC = (
    "在当前对话工作区目录下执行 shell 命令（操作被限制在本工作区内，越界写/删会被拒绝）。\n"
    "渲染 deck：python skills/dazzle-deck/scripts/render_deck.py deck.html shots/ --page N|--all\n"
    "除渲染外，还可用文件处理命令编辑/排查 deck.html，例如 sed、awk、grep、mv、cp、rm、touch、"
    "cat、head、tail、wc、find、diff 等——在大单文件 HTML 上用 sed/awk 批量改写常比 edit 精确匹配更稳。"
    "渲染后用 vision_analyze 看 PNG。禁止网络/安装/提权/系统级破坏性命令。")


def _openai_tools() -> list[dict]:
    """tools.py 的 Anthropic schema → OpenAI function 格式（与训练同一套 6 工具）。
    bash 描述按 studio 放开后的能力覆盖（仅影响发给端点的工具定义，teacher schema 不变）。"""
    out = []
    for t in atools.agent_tools(enable_image_gen=True):
        desc = t.get("description", "")
        if t["name"] == "bash":
            desc = _STUDIO_BASH_DESC
        out.append({"type": "function", "function": {
            "name": t["name"], "description": desc,
            "parameters": t.get("input_schema", {"type": "object", "properties": {}})}})
    return out


BASE_SYSTEM = _load_base_system()
OPENAI_TOOLS = _openai_tools()

# 英文 base system —— 与 ZH BASE_SYSTEM 语义对齐的英文孪生，供英文 query 使用（此 7999
# 为纯 demo/体验站、产物不回流训练，故按语言切 base，让英文全链路语言干净；与静态
# sn-ppt-web 的 BASE_SYSTEM/BASE_SYSTEM_EN 双份同理）。ZH 文件 + _load_base_system 自检保持不动。
BASE_SYSTEM_EN = """\
You are a top-tier front-end engineer and visual designer chasing Awwwards-level visual impact, built to move people: by default make every artifact visually stunning, atmospheric, and memorable, while keeping typography rigorous and content truthful.

How you work:
- Understand the task first. This task has a dedicated Skill: first `read_file skills/dazzle-deck/SKILL.md` to learn its workflow and contract, then execute per that Skill (progressively read_file the reference files it points to, only when needed).
- **Drive autonomously** — do not ask the user questions or pause for confirmation; fill in reasonable assumptions yourself and make tasteful decisions.
- Use the tools to **actually produce files** (plan.md, deck.html), not merely describe them in prose; you must render first, then look at the screenshots to self-check.
- When everything is done, finish with **one short prose summary** — that summary is your final output.

Available Skill:
- dazzle-deck (`skills/dazzle-deck/SKILL.md`): produce a single-HTML dazzling presentation deck (1280×720, 16:9, keyboard paging, immersive motion and cross-slide transitions)."""


def _base_system(language: str) -> str:
    """按 query 语言选 base system：en → BASE_SYSTEM_EN，其余 → 训练同款中文 BASE_SYSTEM。"""
    return BASE_SYSTEM_EN if str(language or "").lower() == "en" else BASE_SYSTEM



def _runtime_time_context(started_epoch: float, language: str = "zh") -> str:
    """权威时间上下文，与静态 sn-ppt-web 的 _runtime_time_context 对齐：给整棵 agent 树
    一个统一的任务开始时间戳，避免模型用记忆里的当前日期误判已发生/当前/未来与资料日期。"""
    started_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(started_epoch)))
    if language == "en":
        return (
            f"Authoritative time context: this task started at {started_utc}. "
            "Use this timestamp for past/current/future and source-date judgments; "
            "do not rely on a remembered current date."
        )
    return (
        f"权威时间上下文：本任务开始于 {started_utc}。判断已发生、当前、未来和"
        "资料日期时以此为准，不使用模型记忆中的当前日期。"
    )


def _infer_prompt_language(text) -> str:
    """按 query 主语言判 zh/en（与静态 sn-ppt-web 同款启发式）：无 CJK→en；无拉丁→zh；
    混排按 CJK×4 权重（CJK 单字信息密度远高于拉丁词），避免英文技术名词把中文 query 判成 en。"""
    value = str(text or "")
    cjk = len(re.findall(r"[㐀-鿿]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if not cjk:
        return "en"
    if not latin:
        return "zh"
    return "zh" if cjk * 4 >= latin else "en"


def _visible_response_language_context(language: str) -> str:
    """可见回复语言硬指令（与静态 sn-ppt-web 对齐）：deck 正文 + 过程说明跟 query 语言走，
    不做中英并行。给弱模型一条比 SKILL.md 更硬的约束，防中英混排。"""
    if str(language or "").lower() == "en":
        return (
            "Visible response language: use English for progress notes, any visible "
            "reasoning/thinking, tool-call preambles, and final summaries. Code, paths, "
            "quotations, and proper nouns may remain in their original language. "
            "The deck's on-screen text language follows the user's explicit delivery "
            "requirement; when the request does not specify one, default to this query's "
            "language. Whatever language the deck uses, keep it consistent across the "
            "whole deck — do not produce parallel bilingual copy."
        )
    return (
        "可见回复语言：过程说明、可见的 reasoning/thinking、工具调用前说明和最终总结使用中文；"
        "代码、路径、原文与专有名词可保留原文。"
        "deck 屏显正文语言服从用户在需求里明确指定的交付语言；未指定时默认跟随本 query 的语言。"
        "无论 deck 用哪种语言，整册保持统一、不做中英并行两套文本。"
    )



_RE_THINK = re.compile(r"<think>.*?</think>", re.S)
_RE_TOOLCALL = re.compile(r"<tool_call>.*?</tool_call>", re.S)


def _narration(content: str) -> str:
    """从模型 content 里剥掉空 think 与 <tool_call> 块，留调用前写的自然语言（展示/存储用）。"""
    s = _RE_TOOLCALL.sub("", _RE_THINK.sub("", content or ""))
    return s.strip()


# ─────────────────────────────────────────────── 会话级 agent 上下文（给 tools.py 用）
class StudioAgent:
    """tools.dispatch 需要的最小上下文：沙箱路径 + 渲染/视觉/生图配置 + 计数器。"""

    def __init__(self, ws: str, enable_image_gen: bool):
        self.ws = os.path.abspath(ws)
        self.sub_dir = os.path.join(self.ws, "_trace", "agent")
        os.makedirs(os.path.join(self.sub_dir, "images"), exist_ok=True)
        self.render_script = acfg.RENDER_SCRIPT_REL
        self.bash_timeout = acfg.BASH_TIMEOUT_S
        self.max_vision_edge = acfg.MAX_VISION_EDGE
        # studio 在线体验：放开 bash（sed/awk/mv/cp/rm 等），靠 tools.bash 的路径守卫关在本工作区内。
        # teacher 造数据的 Agent 不设此属性 → bash 行为与改动前逐字节一致。
        self.bash_relaxed = True
        self.enable_image_gen = enable_image_gen
        self.img_base = os.environ.get("OPENAI_BASE_URL", acfg.OPENAI_BASE_URL).rstrip("/")
        self.img_key = os.environ.get("OPENAI_API_KEY", "")
        self.image_model = os.environ.get("IMAGE_MODEL", acfg.IMAGE_MODEL)
        self.img_n = 0
        self.view_n = 0

    def safe(self, path):
        p = os.path.normpath(os.path.join(self.ws, path))
        if p != os.path.normpath(self.ws) and not p.startswith(os.path.normpath(self.ws) + os.sep):
            raise ValueError(f"路径越出工作区: {path}")
        return p

    def read_path(self, path):
        if path == "skills" or path.startswith("skills/"):
            # agent 用相对前缀 "skills/..." 访问 skill 树；把该前缀映射到 SKILLS_DIR
            # （可被 AGENTIC_SKILLS_DIR 覆盖指向副本），而不是硬编码 ROOT/skills。
            # 对齐 agent_loop.py：剥掉 "skills" 前缀后用 SKILLS_DIR 拼，否则 SKILLS_DIR≠ROOT/skills 时误判越界。
            sk = os.path.normpath(SKILLS_DIR)
            rel = path[len("skills"):].lstrip("/")   # 剥掉 "skills" 前缀，余下相对 skill 树根
            p = os.path.normpath(os.path.join(sk, rel))
            if p != sk and not p.startswith(sk + os.sep):
                raise ValueError("路径越出 skills")
            return p
        return self.safe(path)


def _link_skills(ws: str):
    link = os.path.join(ws, "skills")
    if not os.path.lexists(link):
        try:
            os.symlink(SKILLS_DIR, link)
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────── 友好标签（面向大众）
def _page_from_render(cmd: str):
    m = re.search(r"--page\s+(\d+)", cmd or "")
    if m:
        return int(m.group(1))
    if "--all" in (cmd or ""):
        return "all"
    return None


def friendly(name: str, args: dict):
    path = (args.get("path") or "")
    if name == "read_file":
        if "SKILL.md" in path:
            return "📖", "阅读 dazzle 设计手册"
        if "references/" in path:
            return "📖", f"查阅参考资料（{os.path.basename(path)}）"
        if path.endswith("deck.html"):
            return "🔍", "回看 deck 代码"
        return "📖", f"读取 {path or '文件'}"
    if name == "write_file":
        if path.endswith("plan.md"):
            return "🧭", "规划 deck 结构"
        if path.endswith("deck.html"):
            return "🎨", "搭建 deck 骨架"
        if path.startswith("assets/"):
            return "💾", f"写入素材 {os.path.basename(path)}"
        return "📝", f"写入 {path or '文件'}"
    if name == "edit":
        if path.endswith("deck.html"):
            return "✏️", "编写 / 调整页面"
        return "✏️", f"编辑 {path or '文件'}"
    if name == "bash":
        p = _page_from_render(args.get("command", ""))
        if p == "all":
            return "📸", "渲染全部页面"
        if p:
            return "📸", f"渲染第 {p} 页"
        return "⚙️", "执行命令"
    if name == "vision_analyze":
        return "👀", "检查页面渲染效果"
    if name == "image_generate":
        return "🖼️", "生成配图素材"
    return "•", name


# ─────────────────────────────────────────────── 会话存储 + 事件
_LOCK = threading.Lock()
_CONVS = {}   # conv_id -> {"status","thread","stop"}
_APPEND_LOCK = threading.Lock()
_SEQ = {}     # conv_id -> 单调事件序号（前端据此去重，防 SSE 重连重复）


def _conv_dir(conv_id: str) -> Path:
    return STUDIO_DIR / conv_id


def _events_path(conv_id: str) -> Path:
    return _conv_dir(conv_id) / "events.jsonl"


def _meta_path(conv_id: str) -> Path:
    return _conv_dir(conv_id) / "meta.json"


def _messages_path(conv_id: str) -> Path:
    return _conv_dir(conv_id) / "messages.json"


def _append_event(conv_id: str, ev: dict):
    with _APPEND_LOCK:
        if conv_id not in _SEQ:   # 首次：从已有事件文件行数播种（续编/重启续号）
            p = _events_path(conv_id)
            _SEQ[conv_id] = sum(1 for _ in open(p, encoding="utf-8")) if p.exists() else 0
        _SEQ[conv_id] += 1
        # conv_id 进每个事件：前端 SSE 收到后校验归属，杜绝旧流/并发对话事件串台。
        ev = {"seq": _SEQ[conv_id], "conv_id": conv_id, "ts": time.time(), **ev}
        with open(_events_path(conv_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()


def _read_meta(conv_id: str) -> dict:
    try:
        return json.loads(_meta_path(conv_id).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_meta(conv_id: str, meta: dict):
    _meta_path(conv_id).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def update_meta_fields(conv_id: str, **fields):
    """读-改-写 meta 的若干字段(移植进 Sensenova-WebUI 时补;dynamic.py:408 会调,
    用于异步回填 title 等)。"""
    meta = _read_meta(conv_id)
    meta.update(fields)
    _write_meta(conv_id, meta)
    return meta


def list_conversations(viewer=None, is_admin=False):
    """列会话。viewer 非空时只返回 owner==viewer 的（is_admin=True 则返回全部）。
    viewer=None 时返回全部（供内部/迁移用；对外路由务必传 viewer 以做归属隔离）。"""
    out = []
    if STUDIO_DIR.is_dir():
        for d in STUDIO_DIR.iterdir():
            if not d.is_dir():
                continue
            meta = _read_meta(d.name)
            if not meta:
                continue
            if viewer is not None and not is_admin and meta.get("owner") != viewer:
                continue
            out.append({"conv_id": d.name, "title": meta.get("title") or "未命名",
                        "created": meta.get("created"), "updated": meta.get("updated"),
                        "owner": meta.get("owner") or "",
                        "model_key": meta.get("model_key") or "", "model_label": meta.get("model_label") or "",
                        "status": _CONVS.get(d.name, {}).get("status") or meta.get("status") or "idle"})
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def conv_owner(conv_id: str):
    """读某会话的归属人（meta.owner）；无则 None。供路由做归属校验。"""
    if not conv_id:
        return None
    return _read_meta(conv_id).get("owner") or None


def backfill_owner(default_owner: str):
    """给磁盘上缺 owner 的旧会话补一个默认归属人（幂等）。启动时调一次，迁移历史对话。"""
    if not (default_owner and STUDIO_DIR.is_dir()):
        return
    n = 0
    for d in STUDIO_DIR.iterdir():
        if not d.is_dir():
            continue
        meta = _read_meta(d.name)
        if meta and not meta.get("owner"):
            meta["owner"] = default_owner
            _write_meta(d.name, meta)
            n += 1
    if n:
        print(f"[studio] 归属迁移：{n} 个旧对话归到 '{default_owner}' 名下")


def _reconcile_orphaned():
    """进程启动时调用：磁盘上标 running 但本进程 _CONVS 里没有活线程的对话 = 上个进程被重启/杀掉留下的孤儿，
    标为 interrupted（否则 UI 永远转圈、vLLM 也收不到请求）。仅在启动(_CONVS 空)时安全。"""
    if not STUDIO_DIR.is_dir():
        return
    n = 0
    for d in STUDIO_DIR.iterdir():
        if not d.is_dir() or d.name in _CONVS:
            continue
        meta = _read_meta(d.name)
        if meta.get("status") == "running":
            meta["status"] = "interrupted"
            _write_meta(d.name, meta)
            n += 1
    if n:
        print(f"[studio] 启动自愈：{n} 个被中断的对话标为 interrupted")


def read_events(conv_id: str):
    p = _events_path(conv_id)
    evs = []
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                evs.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                pass
    return evs


def _save_messages(conv_id: str, messages):
    _messages_path(conv_id).write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
    _touch(conv_id)


def _touch(conv_id: str, status: str | None = None):
    meta = _read_meta(conv_id)
    meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if status:
        meta["status"] = status
    _write_meta(conv_id, meta)


# ─────────────────────────────────────────────── 模型调用（OpenAI 流式 + 结构化 tool_calls）
def _est_tokens(messages) -> int:
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c) // 3
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    n += len(part.get("text", "")) // 3
                elif part.get("type") == "image_url":
                    n += IMAGE_TOKENS
        for tc in (m.get("tool_calls") or []):
            n += len(json.dumps(tc, ensure_ascii=False)) // 3
    return n


# 图片发送模式：messages.json 始终存干净的 OpenAI image_url(本地路径)；发送时按后端适配——
#  swift_images（默认，适配 swift pt 部署）：把 image_url 抽成顶层 images=[路径] + content 里留 <image> 占位
#    （swift 跨 role 从扁平 images 列表消费，与训练一致；★swift pt 吃不了 image_url base64，会卡死）。
#  openai_url（vLLM/标准 OpenAI 端点）：保留 image_url inline（本地路径转 base64 data URI）。
IMG_MODE = os.environ.get("STUDIO_IMG_MODE", "swift_images")


def _img_to_data_uri(url: str) -> str:
    if url.startswith("data:") or url.startswith("http"):
        return url
    try:
        with open(url, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception:  # noqa: BLE001
        return url


def _prep_for_send(messages, img_mode=None):
    """返回 (send_messages, images)。swift_images 模式把 image_url → <image> 占位 + 顶层 images=[路径]；
    openai_url 模式保留 image_url（转 base64）、images 为空。img_mode 缺省回退全局 IMG_MODE（逐模型可覆盖）。"""
    img_mode = img_mode or IMG_MODE
    out, images = [], []
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        if img_mode == "openai_url":
            nc = []
            for part in c:
                if part.get("type") == "image_url":
                    u = (part.get("image_url") or {}).get("url", "")
                    nc.append({"type": "image_url", "image_url": {"url": _img_to_data_uri(u)}})
                else:
                    nc.append(part)
            out.append({**m, "content": nc})
        else:   # swift_images
            parts = []
            for part in c:
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    images.append((part.get("image_url") or {}).get("url", ""))
                    parts.append("<image>")
            out.append({**m, "content": "\n".join(p for p in parts if p)})
    return out, images


# 429（限流/过载）退避重试参数。外部托管 API（Moonshot 等）会在自身推理集群繁忙时返
# engine_overloaded_error(429)、或 key 额度触顶时返 rate_limit_reached_error(429)——两者都是
# 服务端正常返回的 HTTP 429（非连接错误），指数退避重试常能自愈过载；限速则退避也未必好转，但仍
# 多等几轮给窗口滑动的机会。内网 vLLM/swift 端点一般不返 429，此逻辑对它们无副作用。
RETRY_MAX_ATTEMPTS = int(os.environ.get("STUDIO_RETRY_ATTEMPTS", "6"))   # 总尝试次数（含首发）
RETRY_429_BASE_S = float(os.environ.get("STUDIO_RETRY_429_BASE", "5"))   # 429 退避基数（秒），指数增长封顶 60s


def _http_error_detail(err) -> tuple[int | None, str, str]:
    """从 requests.HTTPError 里抽 (status_code, err_type, message)。
    Moonshot/OpenAI 风格错误体是 {"error":{"message":..,"type":..}}；抽出来好让前端看清是
    'engine_overloaded'(过载,重试可好) 还是 'rate_limit_reached'(限速,要提额度/降并发)。"""
    resp = getattr(err, "response", None)
    if resp is None:
        return None, "", str(err)
    code = resp.status_code
    etype, msg = "", ""
    try:
        j = resp.json()
        e = j.get("error") if isinstance(j, dict) else None
        if isinstance(e, dict):
            etype = e.get("type", "") or ""
            msg = e.get("message", "") or ""
    except Exception:  # noqa: BLE001
        msg = (resp.text or "")[:300]
    return code, etype, msg


def _stream_completion(vllm_url, model, messages, max_tokens, on_delta, img_mode=None,
                       api_key="", api_style="", on_retry=None, enable_thinking=False,
                       time_context="", language="zh"):
    """POST /chat/completions(stream) with tools。返回 (content_text, tool_calls)。
    tool_calls = [{"id","name","arguments":dict}]（端点 tool-call 解析器结构化回来）。
    连接级重试：仅收到首字节前的连接错误重试（已流式则不重试，避免半截重复）。
    img_mode 逐模型指定后端格式（openai_url=vLLM / swift_images=swift pt），缺省回退全局 IMG_MODE。
    api_key：外部托管 API（如 Kimi/Moonshot）鉴权用，非空则加 Authorization: Bearer（内网端点无鉴权时传空）。
    api_style：'moonshot' = 严格标准 OpenAI 参数（不发 top_k/repetition_penalty/chat_template_kwargs——
      Moonshot 不认这些非标准字段，且 kimi-k3 思考强制开无法关；temperature 还被强制为 1）；
      其余（含空）= 沿用 vLLM/swift 的完整参数集。"""
    img_mode = img_mode or IMG_MODE
    send_msgs, images = _prep_for_send(messages, img_mode)
    system_content = _base_system(language)
    if time_context:
        system_content = f"{system_content}\n\n{time_context}"
    # Moonshot(kimi-k3) 服务端强制 temperature=1（发其它值报 HTTP 400: invalid temperature:
    # only 1 is allowed for this model）；vLLM/swift 沿用低温预设(0.3)。top_p 两边都吃标准字段。
    temperature = 1.0 if api_style == "moonshot" else TEMPERATURE
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_content}] + send_msgs,
        "tools": OPENAI_TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature, "top_p": TOP_P,
    }
    if api_style != "moonshot":
        # vLLM/swift：完整采样参数集（Qwen WebDev 预设）。Moonshot 只吃标准字段，故跳过。
        body["top_k"] = TOP_K
        body["repetition_penalty"] = REPETITION_PENALTY
    if images:   # swift_images 模式：顶层 images=[本地路径]，与 <image> 占位顺序对齐（GPU pod 经共享盘读）
        body["images"] = images
    if img_mode == "openai_url" and api_style != "moonshot":
        # vLLM 端点：思考开关经 chat_template_kwargs.enable_thinking 注入（flash-lite 唯一生效通道，
        # 顶层 enable_thinking 被静默忽略）；由 WebUI 请求逐会话传入，缺省关（训推对齐 enable_thinking=false）。
        # Moonshot(kimi-k3) 思考强制开、也不认 chat_template_kwargs，故不发。
        body["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    url = vllm_url.rstrip("/") + "/chat/completions"
    req_headers = {"Accept": "text/event-stream"}
    if api_key:
        req_headers["Authorization"] = "Bearer " + api_key
    last_err = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        content = []
        tc_acc = {}     # index -> {"id","name","arguments"}
        got_byte = False
        try:
            with requests.post(url, json=body, stream=True, timeout=(60, 1800),
                               headers=req_headers) as r:
                if r.status_code >= 400:
                    # ★流式(stream=True)下 raise_for_status() 抛错时 body 尚未读取，with 退出即关连接 →
                    #   .response.text 变空、.json() 失败，抽不到 Moonshot 的 error.type。故先主动读 body
                    #   缓存进 r（4xx 错误体很小），再抛 HTTPError，让 _http_error_detail 能解析出 type/message。
                    try:
                        _ = r.content
                    except Exception:  # noqa: BLE001
                        pass
                    r.raise_for_status()
                # ★ decode_unicode=False：拿原始 bytes 自己按 UTF-8 解，绝不让 requests 依 r.encoding
                #   （响应头无 charset 时可能被推断为 Latin-1）猜编码——否则 UTF-8 中文会被当 Latin-1
                #   解成 mojibake（æå·²è¯»å®）。外部 API（Moonshot/Kimi）SSE 头常不带 charset，故必须显式。
                for raw in r.iter_lines(decode_unicode=False):
                    got_byte = True
                    if not raw:
                        continue
                    raw = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
                    if not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:  # noqa: BLE001
                        continue
                    delta = ((obj.get("choices") or [{}])[0]).get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                        if on_delta:
                            on_delta(delta["content"])
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        e = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            e["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            e["name"] = fn["name"]
                        if fn.get("arguments"):
                            e["arguments"] += fn["arguments"]
            calls = []
            for idx in sorted(tc_acc):
                e = tc_acc[idx]
                if not e["name"]:
                    continue
                try:
                    args = json.loads(e["arguments"]) if e["arguments"].strip() else {}
                except Exception:  # noqa: BLE001
                    args = {}
                calls.append({"id": e["id"] or f"call_{idx}", "name": e["name"], "arguments": args})
            return "".join(content), calls
        except requests.HTTPError as e:  # 服务端返回了 HTTP 错误码（有响应体）
            last_err = e
            code, etype, msg = _http_error_detail(e)
            # 429（过载/限速）：服务端 429 常在首字节前抛（got_byte=False）。指数退避重试；
            # Retry-After 头若有则优先尊重（Moonshot 实测无此头，退回指数）。已开始流式(got_byte)
            # 才 429 极罕见，此时重试会重复半截输出 → 不退避、直接抛。
            if code == 429 and not got_byte and attempt < RETRY_MAX_ATTEMPTS - 1:
                ra = 0.0
                try:
                    ra = float((getattr(e, "response", None).headers or {}).get("Retry-After") or 0)
                except Exception:  # noqa: BLE001
                    ra = 0.0
                wait = ra if ra > 0 else min(60.0, RETRY_429_BASE_S * (2 ** attempt))
                if on_retry:
                    reason = "服务繁忙(过载)" if "overload" in etype else (
                             "触发限速" if "rate_limit" in etype else "请求过多(429)")
                    on_retry(attempt + 1, RETRY_MAX_ATTEMPTS, wait, reason)
                time.sleep(wait)
                continue
            # 非 429、或已流式、或重试耗尽：抛出带 Moonshot type/message 的清晰错误给上层→前端。
            tag = f"[{etype}] " if etype else ""
            raise RuntimeError(f"HTTP {code}: {tag}{msg or str(e)}") from e
        except Exception as e:  # noqa: BLE001  连接级错误（超时/断连等）
            last_err = e
            if got_byte or attempt == RETRY_MAX_ATTEMPTS - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise last_err if last_err else RuntimeError("stream failed")


# ─────────────────────────────────────────────── 工具执行
def _deck_url(conv_id: str, ws: str):
    # 本仓动态产物经 /dynamic/files/<cid>/<rel> 路由服务（见 dynamic.py + app.js 的截图加载）；
    # 瑞泽原件用的 /studio/static/ 前缀本仓未挂载，会 404。
    return f"/dynamic/files/{conv_id}/deck.html" if os.path.exists(os.path.join(ws, "deck.html")) else None


def _render_n_pages(ws: str):
    for sub in ("shots", "_accept_shots"):
        rj = os.path.join(ws, sub, "render.json")
        if os.path.exists(rj):
            try:
                return json.loads(open(rj, encoding="utf-8").read()).get("n_pages")
            except Exception:  # noqa: BLE001
                pass
    return None


def _run_one_tool(agent: StudioAgent, conv_id: str, call: dict, emit) -> dict:
    """执行一个工具调用，返回 OpenAI tool 消息 {role:tool, tool_call_id, content}。emit 推过程事件。
    vision/生图返回图：content 用 [text, image_url] 列表（image_url.url 存截图本地路径，发送时转 base64）。"""
    name, args, cid = call["name"], call.get("arguments") or {}, call["id"]
    icon, label = friendly(name, args)
    emit("tool_call", {"name": name, "args": args, "icon": icon, "label": label})

    try:
        res = atools.dispatch(agent, name, args)
    except Exception as e:  # noqa: BLE001
        res = f"{name} 崩溃: {e}"

    # 视觉/生图返回 {image_b64,...}：快照到 _trace/agent/images/，回灌 image_url
    if isinstance(res, dict) and "image_b64" in res:
        agent.view_n += 1
        rel = os.path.join("images", f"view_{agent.view_n:02d}.png")
        abspath = os.path.join(agent.sub_dir, rel)
        try:
            with open(abspath, "wb") as f:
                f.write(base64.b64decode(res["image_b64"]))
        except Exception:  # noqa: BLE001
            abspath = None
        summary = res.get("summary", "")
        shot_url = f"/dynamic/files/{conv_id}/_trace/agent/images/{os.path.basename(rel)}"
        emit("vision", {"path": args.get("path"), "prompt": args.get("prompt"),
                        "img_url": shot_url, "label": label, "icon": icon})
        content = [{"type": "text", "text": summary or "（已查看截图）"}]
        if abspath:
            content.append({"type": "image_url", "image_url": {"url": os.path.abspath(abspath)}})
        return {"role": "tool", "tool_call_id": cid, "content": content}

    text = str(res)[:TOOL_RESULT_CAP]

    if name == "bash" and "render_deck.py" in str(args.get("command", "")) and not text.startswith("bash 错误"):
        page = _page_from_render(args.get("command", ""))
        emit("page_rendered", {"page": (page if isinstance(page, int) else None),
                               "n_pages": _render_n_pages(agent.ws),
                               "deck_url": _deck_url(conv_id, agent.ws),
                               "slide": (page if isinstance(page, int) else None)})
    if name in ("write_file", "edit") and str(args.get("path", "")).endswith("deck.html"):
        emit("deck_update", {"deck_url": _deck_url(conv_id, agent.ws), "n_pages": _render_n_pages(agent.ws)})

    # 把完整结果（已被 TOOL_RESULT_CAP 截到 8000）发给前端，让读文件/bash 输出能看全；
    # 前端自行折叠/展开，不再在这里砍到 600。
    ok = not str(text).startswith(("bash 错误", f"{name} 错误", f"{name} 崩溃"))
    emit("tool_result", {"name": name, "ok": ok, "excerpt": text, "icon": icon, "label": label})
    return {"role": "tool", "tool_call_id": cid, "content": text or "(no output)"}


# ─────────────────────────────────────────────── 主循环
def _run_loop(conv_id: str, vllm_url: str, model: str, enable_image_gen: bool, img_mode: str = "",
              api_key: str = "", api_style: str = "", enable_thinking: bool = False,
              max_tokens: int = 0, max_turns: int = 0):
    ws = str(_conv_dir(conv_id))
    _link_skills(ws)
    agent = StudioAgent(ws, enable_image_gen)
    per_turn_cap = max_tokens if max_tokens and max_tokens > 0 else PER_TURN_MAX_TOKENS
    turn_budget = max_turns if max_turns and max_turns > 0 else MAX_TURNS
    # 权威时间上下文 + 可见语言指令（与静态 sn-ppt-web 对齐）：整轮共用会话创建时的
    # started_epoch 与 prompt_language；语言缺失时按首条 user 消息即时判定（续编老会话兜底）。
    _meta = _read_meta(conv_id)
    _started_epoch = _meta.get("started_epoch") or time.time()
    prompt_language = _meta.get("prompt_language")
    if prompt_language not in {"zh", "en"}:
        try:
            _first_user = next((m for m in json.loads(_messages_path(conv_id).read_text(encoding="utf-8"))
                                if m.get("role") == "user"), {})
            prompt_language = _infer_prompt_language(_first_user.get("content", ""))
        except Exception:  # noqa: BLE001
            prompt_language = "zh"
    time_context = (
        f"{_runtime_time_context(_started_epoch, prompt_language)}\n"
        f"{_visible_response_language_context(prompt_language)}"
    )

    def emit(kind, payload):
        _append_event(conv_id, {"kind": kind, **payload})

    messages = json.loads(_messages_path(conv_id).read_text(encoding="utf-8"))
    # 续编时恢复截图计数（避免覆盖已有 view_NN.png）
    try:
        imgs = os.listdir(os.path.join(agent.sub_dir, "images"))
        agent.view_n = len([x for x in imgs if x.startswith("view_")])
    except Exception:  # noqa: BLE001
        pass

    heals = 0
    did_render_all = False   # 模型做过 render_deck --all（SKILL 全局复审）才算接近完成、允许收尾
    try:
        for turn in range(turn_budget):
            if _CONVS.get(conv_id, {}).get("stop"):
                emit("done", {"status": "stopped", "reason": "用户停止"})
                _touch(conv_id, status="stopped")
                return
            max_tok = max(2000, min(per_turn_cap, CTX_LIMIT - _est_tokens(messages) - CTX_HEADROOM))
            emit("turn_start", {"turn": turn})

            def on_delta(chunk):
                emit("assistant_delta", {"text": chunk})

            def on_retry(n, total, wait, reason):
                emit("note", {"text": f"模型{reason}，{int(wait)}s 后自动重试（{n}/{total-1}）…"})

            try:
                content, calls = _stream_completion(vllm_url, model, messages, max_tok, on_delta, img_mode,
                                                    api_key=api_key, api_style=api_style, on_retry=on_retry,
                                                    enable_thinking=enable_thinking, time_context=time_context,
                                                    language=prompt_language)
            except Exception as e:  # noqa: BLE001
                emit("error", {"message": f"模型调用失败：{type(e).__name__}: {str(e)[:300]}"})
                emit("done", {"status": "error", "reason": "model_call_failed"})
                _touch(conv_id, status="error")
                return

            narration = _narration(content)
            # 回灌 assistant（标准 OpenAI：content=叙述文字，tool_calls 结构化）
            am = {"role": "assistant", "content": narration}
            if calls:
                am["tool_calls"] = [{"id": c["id"], "type": "function",
                                     "function": {"name": c["name"],
                                                  "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                                    for c in calls]
            messages.append(am)

            if not calls:
                # 只有做过全局 render --all（接近完成）或反复提示无效才允许收尾；否则"光说不做"→提示继续。
                if did_render_all or heals >= MAX_HEALS:
                    emit("final", {"text": narration or content.strip()})
                    emit("done", {"status": "completed", "reason": "text_response"})
                    _touch(conv_id, status="completed")
                    _save_messages(conv_id, messages)
                    return
                heals += 1
                if narration:
                    emit("assistant_text", {"text": narration})
                emit("note", {"text": f"继续按流程推进（提示 {heals}/{MAX_HEALS}）"})
                messages.append({"role": "user", "content": NUDGE_PROMPT})
                _save_messages(conv_id, messages)
                continue

            heals = 0   # 调了工具=有进展，重置提示计数
            if narration:
                emit("assistant_text", {"text": narration})

            for c in calls:
                if c["name"] == "bash" and _page_from_render((c.get("arguments") or {}).get("command", "")) == "all":
                    did_render_all = True
                tmsg = _run_one_tool(agent, conv_id, c, emit)
                messages.append(tmsg)
            _save_messages(conv_id, messages)
        else:
            emit("done", {"status": "max_turns", "reason": f"到达 max_turns({MAX_TURNS})"})
            _touch(conv_id, status="max_turns")
    finally:
        with _LOCK:
            st = _CONVS.get(conv_id)
            if st:
                st["status"] = _read_meta(conv_id).get("status") or "idle"


# ─────────────────────────────────────────────── 对外 API
def send_message(conv_id, message: str, vllm_url: str, model: str, enable_image_gen: bool = True,
                 model_key: str = "", model_label: str = "", img_mode: str = "", owner: str = "",
                 api_key: str = "", api_style: str = "",
                 thinking: bool = False, thinking_transport: bool = False,
                 requested_thinking: bool = False, generation_preferences: dict = None,
                 tool_config: dict = None, max_tokens: int = 0, max_turns: int = 0) -> str:
    message = (message or "").strip()
    if not message:
        raise ValueError("message 不能为空")
    new = conv_id is None or not _conv_dir(conv_id).is_dir()
    if new:
        conv_id = uuid.uuid4().hex[:16]
        _conv_dir(conv_id).mkdir(parents=True, exist_ok=True)
        prompt_language = _infer_prompt_language(message)
        _write_meta(conv_id, {"title": message[:40], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "running",
                              "owner": owner or "", "started_epoch": time.time(),
                              "prompt_language": prompt_language,
                              "model_key": model_key, "model_label": model_label})
        messages = [{"role": "user", "content": f"{message}\n\n{_harness_suffix(prompt_language)}"}]
        _save_messages(conv_id, messages)
        _append_event(conv_id, {"kind": "user", "text": message, "first": True})
    else:
        messages = json.loads(_messages_path(conv_id).read_text(encoding="utf-8"))
        messages.append({"role": "user", "content": message})   # 续编：无 harness 后缀
        _save_messages(conv_id, messages)
        _append_event(conv_id, {"kind": "user", "text": message, "first": False})
        _touch(conv_id, status="running")

    with _LOCK:
        st = _CONVS.get(conv_id)
        if st and st.get("thread") and st["thread"].is_alive():
            raise RuntimeError("该会话正在生成中，请等当前任务完成")
        th = threading.Thread(target=_run_loop,
                              args=(conv_id, vllm_url, model, enable_image_gen, img_mode, api_key, api_style),
                              kwargs={"enable_thinking": bool(thinking),
                                      "max_tokens": int(max_tokens or 0),
                                      "max_turns": int(max_turns or 0)},
                              daemon=True)
        _CONVS[conv_id] = {"status": "running", "thread": th, "stop": False}
        th.start()
    return conv_id


def stop_conversation(conv_id: str):
    with _LOCK:
        if conv_id in _CONVS:
            _CONVS[conv_id]["stop"] = True


def conversation_active(conv_id: str) -> bool:
    st = _CONVS.get(conv_id)
    return bool(st and st.get("thread") and st["thread"].is_alive())


def delete_conversation(conv_id: str) -> dict:
    """删除整个会话目录（studio_runs/<conv_id>）。生成中拒绝删除。返回 {ok|error}。"""
    import shutil
    if conversation_active(conv_id):
        return {"error": "该会话正在生成中，请先停止再删除"}
    d = _conv_dir(conv_id)
    base = STUDIO_DIR.resolve()
    target = d.resolve()
    if base != target.parent or not target.is_dir():
        return {"error": "会话不存在"}
    try:
        shutil.rmtree(target)
    except Exception as e:  # noqa: BLE001
        return {"error": f"删除失败：{e}"}
    with _LOCK:
        _CONVS.pop(conv_id, None)
    with _APPEND_LOCK:
        _SEQ.pop(conv_id, None)
    return {"ok": True}


# public 别名:Sensenova-WebUI 的 studio/app/dynamic.py 以 runtime.reconcile_orphaned() 调用。
def reconcile_orphaned():
    """启动时把上个进程留下的孤儿 running 对话标为 interrupted。
    移植进 Sensenova-WebUI 时暴露为 public;调用时机由 main.py lifespan 控制
    (原模块加载即自动调会误杀别进程的在飞 job,故此处不再 import-time 自动执行)。"""
    return _reconcile_orphaned()


# 注:原瑞泽版在此处 import-time 调 _reconcile_orphaned();移植后交由 Sensenova
# main.py 的 lifespan 在确认本进程拥有 dynamic worker 时显式调用 reconcile_orphaned()。
