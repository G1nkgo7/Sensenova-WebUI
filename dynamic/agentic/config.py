"""agentic 管线专属配置（与旧 stage* 管线平行，互不影响）。

模型/base_url/价格表复用仓库根 config.py；这里只放多轮 agent 特有的常量。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
# 移植进 Sensenova-WebUI 时砍掉对瑞泽仓库根 config.py 的依赖(仅用到 2 个 ANTHROPIC 常量),
# 改为直接读环境变量(Sensenova 的 .env / launch.py 已注入 ANTHROPIC_*)。

# ── 路径 ───────────────────────────────────────────────────────────
RUNS_DIR = HERE / "runs"            # runs/<batch>/<sid>/{deck.html, assets/, plan.md, shots/, _trace/}
QUERIES_DIR = HERE / "queries"      # query_gen 产物 <batch>.jsonl + _style_ledger.json
DATA_DIR = HERE / "data_openai"     # SFT 导出
LOGS_DIR = HERE / "logs"            # <batch>.manifest.jsonl
SKILLS_DIR = Path(os.environ.get("AGENTIC_SKILLS_DIR", ROOT / "skills"))  # dazzle-deck skill 树（agent 通过软链以相对路径访问）；AGENTIC_SKILLS_DIR 可覆盖，供 A/B 测试指向不同 skill 副本
SKILL_NAME = "dazzle-deck"
RENDER_SCRIPT_REL = f"skills/{SKILL_NAME}/scripts/render_deck.py"

# ── 模型（复用根 config）───────────────────────────────────────────
ANTHROPIC_MODEL = os.environ.get("SENSENOVA_ANTHROPIC_NAME") or os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7-thinking")          # teacher
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com")

# ═══════════════════════════════════════════════════════════════════════
# 后端切换：Anthropic(默认，用于 Claude 造训练数据) / OpenAI 兼容端点(测试用)
# ═══════════════════════════════════════════════════════════════════════
# ★★★ 用 Claude(Opus)造数据的人请先读这段 ★★★
# 「换模型」这套改动是【默认关闭、显式才开】的旁路，不会污染你的 Claude 跑批：
#   · 不设 MODEL_BACKEND 且 model 名含 claude/opus/sonnet/haiku → 永远走 Anthropic 原路径，
#     行为与加这套代码之前【一字不差】（水位用下方 TEACHER_CTX_*，记账走真实 PRICING）。
#   · 只有满足下列任一条件才会切到 nova/OpenAI 后端：
#       (a) 环境变量 MODEL_BACKEND=openai；或
#       (b) 显式把 MODEL 设成一个不含 claude/opus/sonnet/haiku 的名字（启发式判成 openai）。
# ★ 唯一的真实风险 = 有人把 MODEL_BACKEND=openai 写进了 .env（load_dotenv 会让 .env 覆盖一切，
#   见 run_batch.load_dotenv）。所以【切勿把 MODEL_BACKEND/LLM_* 落进 .env】——只在跑 nova 测试的
#   那一条命令前临时 export，用完即散。Claude 造数据时 .env 里不该出现 MODEL_BACKEND。
# 自检：想确认某次跑批走哪个后端，看 run_batch 启动打印的 “🔌 backend=...” 行即可。
# ───────────────────────────────────────────────────────────────────────
# model_call.call_with_tools 按 MODEL_BACKEND 分派：默认 anthropic；设 openai 时走裸 httpx 打
# OpenAI /chat/completions（如同事自部署的 nova-27b / lightllm）。见 model_call._backend()。
MODEL_BACKEND = os.environ.get("MODEL_BACKEND", "").strip().lower()
# LLM 端点独立于生图的 OPENAI_BASE_URL（那个被 image_generate 占用，见下方 §工具）——切勿复用。
LLM_OPENAI_BASE_URL = os.environ.get("LLM_OPENAI_BASE_URL", "http://10.119.29.102:8000/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()   # lightllm 无鉴权 → 留空；外部托管才填
NOVA_MODEL = os.environ.get("NOVA_MODEL", "nova-27b-v39-step4k-dpov2")
# nova 上下文 256K：单列一组水位，【不动】下方 Opus 的 TEACHER_CTX_*（agent_loop 按后端择一组）。
# 为什么必须单列而非复用 Opus 的 320K：_clamp_max_tokens 用「窗口−已用输入−余量」算每轮可生成量，
# 若 nova 用 320K 会误以为还能生成一大堆 → 某轮 input+output 超过 nova 真实的 256K → 端点 400 崩。
NOVA_CTX_WINDOW = int(os.environ.get("NOVA_CTX_WINDOW", "256000"))
NOVA_CTX_FORCE = int(os.environ.get("NOVA_CTX_FORCE", "200000"))
NOVA_CTX_WARN = int(os.environ.get("NOVA_CTX_WARN", "150000"))

# ── 多轮循环 ───────────────────────────────────────────────────────
# 90 偏低：强化逐页流程下多页/生图 deck 的 41+ edit 会顶到 90 被强制收尾(b1 有 4 个撞上限)。
# 曾提到 120；2026-07-22 再提到 150——高仿真(v1.2.0)复杂材质题材(gothic/glasstower 等)在
# 120 轮内常"想做更精但没收敛"被拒，给足头部空间让其自然收尾。
MAX_TURNS = int(os.environ.get("AGENTIC_MAX_TURNS", "150"))
# 单轮 max_tokens 对齐学生模型 Qwen 3.5 的单轮输出上限（64K）；teacher Opus 4.7 也写得下。
PER_TURN_MAX_TOKENS = int(os.environ.get("AGENTIC_MAX_TOKENS", "64000"))
THINK_EFFORT = os.environ.get("AGENTIC_EFFORT", "high")
WALL_TIMEOUT_S = int(os.environ.get("AGENTIC_WALL_TIMEOUT", "4500"))   # 单 deck 硬超时 75min
MAX_HEALS = int(os.environ.get("AGENTIC_MAX_HEALS", "2"))             # 空/截断回合的有限自愈

# ── 上下文水位 ─────────────────────────────────────────────────────
# 2026-07-29 实测 tokenhub 的 claude-opus-5 接受 ≥381K 输入（无 200K 硬顶），窗口值取 320K：
# 覆盖 CTX_FORCE(245K) + 单轮最大输出(64K)，_clamp_max_tokens 用它防 input+output 超窗。
# CTX_FORCE 取 245K 而非更高：轨迹总长 ≈ 强制线输入 + 收尾输出，须落进学生 Qwen 3.5 的 256K 窗口，
# 撞线轨迹才不会在 SFT 侧被截断。CTX_WARN 维持 180K（提早注入收敛提示，留足复审空间）。
TEACHER_CTX_WINDOW = int(os.environ.get("AGENTIC_TEACHER_CTX_WINDOW", "320000"))
CTX_WARN = int(os.environ.get("AGENTIC_CTX_WARN", "180000"))    # 注入"收敛进入复审"提示
CTX_FORCE = int(os.environ.get("AGENTIC_CTX_FORCE", "245000"))  # 强制去工具总结收尾
CTX_TOKEN_HEADROOM = 4000   # max_tokens 自适应钳制的安全余量

# ── 工具 ───────────────────────────────────────────────────────────
# 生图开关：默认开。我方 ANTHROPIC_API_KEY 无 gpt-image-2 权限，需单独的 OPENAI_API_KEY
# （见 .env 的 OPENAI_API_KEY/OPENAI_BASE_URL/IMAGE_MODEL）。端点不可用时设 0 撤下工具。
ENABLE_IMAGE_GEN = os.environ.get("ENABLE_IMAGE_GEN", "1") != "0"
# 联网查证开关：web_search 走 Serper，必须有 SERPER_API_KEY（.env）；无 key 自动整组撤下
# web_search/web_fetch（不做免 key 降级搜索，保证训练轨迹来源纯净）。ENABLE_WEB=0 可强制关闭。
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "").strip()
ENABLE_WEB = os.environ.get("ENABLE_WEB", "1") != "0" and bool(SERPER_API_KEY)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
BASH_TIMEOUT_S = int(os.environ.get("AGENTIC_BASH_TIMEOUT", "180"))    # 全页渲染需要更长
MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1280"))       # deck 截图本就 1280×720

# ── 画布 ───────────────────────────────────────────────────────────
DECK_W, DECK_H = 1280, 720
