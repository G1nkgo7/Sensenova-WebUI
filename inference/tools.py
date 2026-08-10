#!/usr/bin/env python3
"""工具实现 + 工具 schema(供 agent_loop 使用)。

Hermes 风格:工具是**自由函数**,操作一个 `agent` 上下文对象(状态),而不是绑在它
上面的方法。每个工具第一个参数都是 `agent`,可读写 agent 的计数器、用 agent 的沙箱
辅助方法。循环里调用 dispatch(agent, name, args)。

工具按角色分组(对应 ppt-skill 的多 Agent 设计):编排器只规划+委派,生产活按 toolsets 委派给专门 subagent。
    编排器(orchestrator): read write edit delegate_task            # 只 file + delegation,不调研/出图/看图
    调研 subagent:         read write web_search web_fetch         # web+file → fact pack 落盘 research/research_NN.md
    配图 subagent:         read image_generate vision_analyze      # image_gen+vision → 配图路径清单
    slide subagent:        read write edit bash vision_analyze     # file+terminal+vision → 写/渲/自纠
    复审 subagent:         read vision_analyze                     # vision → 问题清单
`delegate_task`(委派 subagent)需要递归跑子循环,**实现在 agent_loop 里**,通过
agent.extra_tools 注册;dispatch 会先查 extra_tools,再查这里的 BUILTINS。它的 schema
在本文件 DELEGATE_TASK_SCHEMA 里定义,和其它 schema 放一处避免漂移。

------------------------------------------------------------------ agent 上下文契约
工具依赖 agent 提供(由 agent_loop 实现):
    agent.ws            工作区绝对路径(= 该 sample 的 run_dir)
    agent.safe(rel)     解析一个**可写/渲染**路径到 ws 内;越界则抛错
    agent.read_path(rel)解析一个**可读**路径:ws 内,或只读的 skill 目录
    agent.serper        serper API key(搜文/搜图),无则 None
    agent.img_base/img_key/image_model   OpenAI 兼容的图像生成端点
    agent.img_n         图片计数器(image_generate 自增)
    agent.extra_tools   仅编排器有的额外工具(delegate_task),dispatch 优先查它
所有 key 从环境变量读(由 /tmp/ppt_keys.sh 注入,绝不写进仓库)。
"""
import base64
import json
import os
import re
import subprocess
import urllib.request

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# 渲染逻辑放在 skill 自带脚本 skills/ppt-skill/scripts/render.py 里(可移植):
# 不再有专用 render 工具,subagent 用 `bash` 跑它。


# =============================================================== 叶子工具实现

def read(agent, path, offset=None, limit=None):
    """只读文本(图片请用 vision_analyze)。可读 ws 内文件,也可读只读的 skill 目录。
    路径是目录时,返回该目录下的条目清单(相当于 ls,方便发现 assets/ 里有哪些图)。"""
    if os.path.splitext(path)[1].lower() in IMG_EXT:
        return f"read 错误:'{path}' 是图片,请用 vision_analyze 查看。"
    fp = agent.read_path(path)
    if os.path.isdir(fp):
        entries = sorted(os.listdir(fp))
        if not entries:
            return f"{path}/ (空目录)"
        listing = [e + ("/" if os.path.isdir(os.path.join(fp, e)) else "") for e in entries]
        return f"{path}/ 下的条目:\n" + "\n".join(listing)
    with open(fp, encoding="utf-8") as f:
        all_lines = f.read().splitlines()
    total = len(all_lines)
    start = int(offset) if offset else 1
    lines = all_lines[start - 1:]
    if limit:
        lines = lines[:int(limit)]
    text = "\n".join(lines)
    # 大文件在**行边界**截断 + 明确续读提示(取代旧的 50000 盲切;也压在 _run_tools 的 8000 盲切之下,
    # 避免把内容拦腰切在句中)。让 SFT 里"读大文件"是干净的分页,而非无声半截。
    CAP = 7800
    if len(text) > CAP:
        cut = text[:CAP].rsplit("\n", 1)[0]
        last = start + cut.count("\n")           # 本次显示到的文件行号
        text = cut + f"\n\n[… read 截断:已显示第 {start}–{last} 行(文件共 {total} 行);继续读用 offset={last + 1}]"
    return text


def write(agent, path, content):
    if not getattr(agent, "writable", lambda p: True)(path):
        return ("write 错误:编排器不能写 slides/ 下的页面 HTML。请用 delegate_task 重新委派该页"
                "(带 note 说明改什么);跨页统一的视觉改动改 base.css。")
    fp = agent.safe(path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {len(content.encode())} 字节到 {path}"


def edit(agent, path, old_string, new_string, replace_all=False):
    if not getattr(agent, "writable", lambda p: True)(path):
        return ("edit 错误:编排器不能改 slides/ 下的页面 HTML。请用 delegate_task 重新委派该页"
                "(带 note 说明改什么);跨页统一的视觉改动改 base.css。")
    fp = agent.safe(path)
    with open(fp, encoding="utf-8") as f:
        s = f.read()
    n = s.count(old_string)
    if n == 0:
        return f"edit 错误:在 {path} 里找不到 old_string"
    if n > 1 and not replace_all:
        return f"edit 错误:old_string 出现了 {n} 次(不唯一);确认要全改请传 replace_all=true"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(s.replace(old_string, new_string))
    return f"已编辑 {path}"


def bash(agent, command, timeout=120):
    """在**工作区目录**下执行一条 shell 命令,返回输出(截断到 8KB)。在子进程里跑,天然并行安全。

    主要用途:跑 skill 自带脚本——尤其是**渲染**某页 HTML 成 PNG:
        python skills/ppt-skill/scripts/render.py slides/slide_NN.html renders/slide_NN.png
    成功时 render.py 把 PNG 路径打到 stdout;然后用 vision_analyze 看那张图。"""
    if not isinstance(command, str) or not command.strip():
        return "bash 错误:command 不能为空"
    try:
        r = subprocess.run(command, shell=True, cwd=agent.ws,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"bash 错误:命令超过 {timeout}s 超时"
    except Exception as e:
        return f"bash 错误:{e}"
    out = (r.stdout or "")
    if r.stderr:
        out += ("\n[stderr] " + r.stderr)
    out = out.strip()
    return out[:8000] if out else f"(命令完成,退出码 {r.returncode},无输出)"


MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1536"))   # 送模型的图最长边上限


def vision_analyze(agent, path, prompt=None):
    """返回图片像素,让模型能**看见**这张截图。返回 dict 由 agent_loop 转成图像内容块。

    渲染图是 2x 高清(3200×1800,可达数 MB),但视觉模型(Bedrock)对单图有 **5MB / 最大尺寸**
    硬限制,且 Anthropic 本来就把图降采样到 ~1568px。所以这里把**送给模型的图**降到最长边
    MAX_VISION_EDGE(磁盘上的 `renders/` 全分辨率原图不动),既绕开 5MB/尺寸 400,又不损模型所见。"""
    fp = agent.read_path(path)              # 一律走沙箱,绝不放行绝对路径(防越界读任意文件)
    if not os.path.exists(fp) or os.path.isdir(fp):
        return f"vision_analyze 错误:没有这张图 {path}"
    try:
        import io
        from PIL import Image
        with Image.open(fp) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = MAX_VISION_EDGE / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
    except ImportError:
        with open(fp, "rb") as f:   # 没装 PIL 时退回原图(小图本就没问题)
            data = f.read()
    except Exception as e:
        # 文件不是合法图片(常见:模型把 .html 当图传进来)。绝不能把原始字节当图发出去
        # ——服务端会 400,连续重试同 payload 直接搞死整条 run。返回文本让模型自纠。
        return (f"vision_analyze 错误:{path} 不是可解析的图片({type(e).__name__})。"
                f"只能查看 PNG/JPG 等图片;HTML 页请先渲染:"
                f"python skills/ppt-skill/scripts/render.py slides/slide_NN.html renders/slide_NN.png")
    return {"image_b64": base64.b64encode(data).decode(), "media_type": "image/png",
            "path": os.path.relpath(fp, agent.ws), "summary": f"正在查看 {path}。"}


def web_search(agent, query, count=5):
    """搜文也搜图(serper)。返回标题 / 链接 / 摘要列表。"""
    if not agent.serper:
        return "web_search 不可用(未配置 serper key)"
    n = min(int(count or 5), 10)
    try:
        base_url = str(
            getattr(agent, "serper_base", "")
            or os.environ.get("SERPER_BASE_URL", "https://google.serper.dev")
        ).rstrip("/")
        endpoint = base_url if base_url.endswith("/search") else f"{base_url}/search"
        r = requests.post(endpoint,
                          headers={"X-API-KEY": agent.serper, "Content-Type": "application/json"},
                          json={"q": query, "num": n}, timeout=30).json()
    except Exception as e:
        return f"web_search 错误:{e}"
    out = []
    for it in r.get("organic", [])[:n]:
        out.append(f"- {it.get('title')}\n  {it.get('link')}\n  {it.get('snippet', '')}")
    for it in r.get("images", [])[:n]:                  # 搜图结果(若有)
        out.append(f"- [图] {it.get('title', '')}\n  {it.get('imageUrl') or it.get('link')}")
    return "\n".join(out) or "(无结果)"


def web_fetch(agent, url):
    """抓一个 URL,抽正文为纯文本(截断到 ~3500 字)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        return f"fetch 错误:{e}"
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()[:3500]


def image_generate(agent, prompt, aspect_ratio="16:9", out_path=None):
    """生成照片 / 插画类配图(不用于图表),存到 assets/,返回相对路径。

    out_path:调用方指定的目标文件名。**并发出图必须传**——并行的 image-curator 各是独立 agent、
    img_n 都从 0 起,不指定路径会全写 assets/img_01.png 互相覆盖。由编排器在规划时给每张图分配
    唯一路径(如 assets/img_05.png),各 curator 写各的,无共享状态、无锁、无撞名,且路径可被
    plan/base.css 确定性引用。只取文件名落到 assets/ 下、强制 .png(挡 ../ 逃逸)。
    不传则回退 img_n 自增(单线程编排器直调的旧行为,无并发)。"""
    size = {"16:9": "1536x1024", "9:16": "1024x1536", "4:3": "1536x1024",
            "3:4": "1024x1536", "1:1": "1024x1024"}.get(aspect_ratio, "1536x1024")
    try:
        d = requests.post(f"{agent.img_base}/images/generations",
                          headers={"Authorization": f"Bearer {agent.img_key}",
                                   "Content-Type": "application/json"},
                          json={"model": agent.image_model, "prompt": prompt, "size": size, "n": 1},
                          timeout=180).json()
    except Exception as e:
        return f"image_generate 错误:{e}"
    if "data" not in d:
        return f"image_generate 错误:{json.dumps(d, ensure_ascii=False)[:200]}"
    it = d["data"][0]
    if it.get("b64_json"):
        data = base64.b64decode(it["b64_json"])
    elif it.get("url"):
        try:
            data = requests.get(it["url"], timeout=120).content
        except Exception as e:
            return f"image_generate 错误:下载图片失败 {e}"
    else:
        return "image_generate 错误:没有返回图片"
    rel = None
    if out_path:                                    # 调用方指定路径(并发安全):只取文件名,落 assets/
        name = os.path.basename(str(out_path)).strip()
        if name:
            if not name.lower().endswith(".png"):
                name += ".png"
            rel = f"assets/{name}"
    if rel is None:                                 # 兜底:旧的 img_n 自增(单线程编排器直调)
        agent.img_n += 1
        rel = f"assets/img_{agent.img_n:02d}.png"
    os.makedirs(agent.safe("assets"), exist_ok=True)
    with open(agent.safe(rel), "wb") as f:
        f.write(data)
    return rel


# =============================================================== 工具 schema(Anthropic 原生格式)

_READ = {
    "name": "read",
    "description": "读文件内容(只读文本;图片请用 vision_analyze)。可读工作区文件,也可读 skill 的 SKILL.md。路径是目录时返回目录下条目清单(相当于 ls,可用来查看 assets/ 里有哪些图)。文本超长会截断到约 50KB。",
    "input_schema": {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "文件路径"},
        "offset": {"type": "number", "description": "起始行(1 开始)"},
        "limit": {"type": "number", "description": "最多读多少行"}}},
}
_WRITE = {
    "name": "write",
    "description": "把内容写入文件;不存在则创建,存在则覆盖,自动建父目录。",
    "input_schema": {"type": "object", "required": ["path", "content"], "properties": {
        "path": {"type": "string", "description": "文件路径(相对工作区)"},
        "content": {"type": "string", "description": "要写入的内容"}}},
}
_EDIT = {
    "name": "edit",
    "description": "把文件里一段精确字符串替换为新字符串。old_string 必须精确匹配且唯一,除非 replace_all=true。看过渲染后做局部修改用它。",
    "input_schema": {"type": "object", "required": ["path", "old_string", "new_string"], "properties": {
        "path": {"type": "string", "description": "要编辑的文件路径"},
        "old_string": {"type": "string", "description": "要替换的精确文本"},
        "new_string": {"type": "string", "description": "替换后的文本"},
        "replace_all": {"type": "boolean", "description": "是否替换全部出现(默认 false)"}}},
}
_BASH = {
    "name": "bash",
    "description": "在工作区目录下执行一条 shell 命令并返回输出。主要用来跑 skill 自带脚本——尤其是**渲染**:`python skills/ppt-skill/scripts/render.py slides/slide_NN.html renders/slide_NN.png`(把某页 HTML 渲成 1600×900 截图,执行 JS、等字体/ECharts 就绪;成功后 stdout 是 PNG 路径)。渲染完用 vision_analyze 看那张 PNG。",
    "input_schema": {"type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令"}}},
}
_VISION = {
    "name": "vision_analyze",
    "description": "查看一张图片(通常是 render 产出的截图):返回图片像素,让你能**看见**它,检查溢出/遮挡/占位/破图/豆腐块等。对 render 返回的路径调用它来审查这一页。",
    "input_schema": {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "图片路径,如 renders/slide_03.png"},
        "prompt": {"type": "string", "description": "可选:想重点看什么"}}},
}
_WEB_SEARCH = {
    "name": "web_search",
    "description": "联网搜索,**可搜文也可搜图**(找事实/数据,或找可参考的视觉/素材)。返回标题、URL、摘要列表。默认 5 条,最多 10。",
    "input_schema": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "搜索词"},
        "count": {"type": "number", "description": "结果条数(默认 5,最多 10)"}}},
}
_WEB_FETCH = {
    "name": "web_fetch",
    "description": "抓取一个 URL 并抽取可读正文(纯文本)。用来深入读 web_search 找到的来源。",
    "input_schema": {"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string", "description": "要抓取的 URL"}}},
}
_IMAGE_GEN = {
    "name": "image_generate",
    "description": "根据文本提示生成图片(照片/插画/主视觉等 CSS 画不出来的视觉)。**不要用于数据图表**(图表用 ECharts)。返回 assets/ 下生成图片的路径。",
    "input_schema": {"type": "object", "required": ["prompt"], "properties": {
        "prompt": {"type": "string", "description": "要生成图片的描述"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "4:3", "1:1", "3:4", "9:16"],
                         "description": "宽高比,默认 16:9"},
        "out_path": {"type": "string", "description": "保存到的文件名,如 assets/img_05.png(只取文件名落到 assets/ 下)。**并行出图时必须由调用方指定唯一路径**以避免撞名;不指定则自动编号。"}}},
}
# delegate_task 的实现在 agent_loop(需递归跑子循环),schema 放这里统一管理。
# 对齐 hermes:子 agent 不是预定义类型,而是调用时由 goal(干啥)+ toolsets(给哪些能力)拼出。
# 加新子 agent 类型 → 改 SKILL.md 的 goal 模板即可,无需动框架。
DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "把任务委派给一个或多个子 agent 并行执行。每个子 agent 在**独立上下文**里跑"
        "(看不到你的对话历史,goal 必须自包含),只把一段文字小结返回给你。\n"
        "- `goal`(必填):这个子 agent 要完成什么,自然语言、自包含。\n"
        "- `context`:背景——文件路径、约束、结构、要改的具体点。\n"
        "- `toolsets`:给它哪些能力包——file(读写文件)/terminal(跑命令,如 render.py)/"
        "vision(看图自检)/image_gen(生成配图)/web(联网检索);缺省给一组通用能力。\n"
        "- `role`:leaf(默认,不能再委派)或 orchestrator(可再起子 agent)。\n"
        "- 一次派多个:传 `tasks` 数组,每条一个子 agent,并行执行。\n"
        "返回 JSON:{\"results\":[{label,status,summary,renders,shot}, …]}。"),
    "input_schema": {"type": "object", "properties": {
        "goal":     {"type": "string", "description": "单个子 agent 要完成什么(自包含)"},
        "context":  {"type": "string", "description": "背景:文件路径/约束/结构/错误信息"},
        "toolsets": {"type": "array", "items": {"type": "string"},
                     "description": "能力包:file/terminal/vision/image_gen/web"},
        "role":     {"type": "string", "enum": ["leaf", "orchestrator"],
                     "description": "默认 leaf(叶子,不能再委派)"},
        "label":    {"type": "string", "description": "可选:子 agent 标签(用于轨迹命名与重试);slide 任务建议填 slide_05"},
        "tasks":    {"type": "array",
                     "description": "批量并行:每条 {goal,context?,toolsets?,role?,label?},各起一个子 agent",
                     "items": {"type": "object", "required": ["goal"], "properties": {
                         "goal":     {"type": "string", "description": "该子 agent 要完成什么(自包含)"},
                         "context":  {"type": "string", "description": "背景"},
                         "toolsets": {"type": "array", "items": {"type": "string"},
                                      "description": "能力包:file/terminal/vision/image_gen/web"},
                         "role":     {"type": "string", "enum": ["leaf", "orchestrator"]},
                         "label":    {"type": "string", "description": "可选标签;slide 任务建议 slide_05"}}}}}},
}


# name -> 实现
BUILTINS = {
    "read": read, "write": write, "edit": edit, "bash": bash,
    "vision_analyze": vision_analyze, "web_search": web_search,
    "web_fetch": web_fetch, "image_generate": image_generate,
}

# name -> schema
SCHEMAS = {s["name"]: s for s in (
    _READ, _WRITE, _EDIT, _BASH, _VISION, _WEB_SEARCH, _WEB_FETCH, _IMAGE_GEN,
    DELEGATE_TASK_SCHEMA)}


# =============================================================== toolset 注册表(对齐 hermes)
# 命名直接照搬 hermes(image_gen 不是 image、terminal 不是 bash),作为插件式扩展的地基:
# delegate_task 的 toolsets 引用这里的能力包名,resolve_toolsets 展开成 schema 列表。
TOOLSETS = {
    "file":       ["read", "write", "edit"],
    "terminal":   ["bash"],
    "vision":     ["vision_analyze"],
    "image_gen":  ["image_generate"],
    "web":        ["web_search", "web_fetch"],
    "delegation": ["delegate_task"],
}
BASE_TOOL_NAMES = ["read"]        # 基础能力:对所有子 agent 默认并入(curator 也常要 read plan/base.css)

# 角色 → 默认 toolsets。编排器只规划+委派:file(写 plan/base.css)+ delegation。
# 调研/配图/复审都已落成专门 subagent,故编排器不再直挂 image_gen/web/vision——这些按 toolsets 委派下去
# (调研→web、配图→image_gen+vision、复审→vision)。不给 terminal——守住"编排器不写 slides / 不执行命令"红线。
ORCHESTRATOR_TOOLSETS = ["file", "delegation"]
SLIDE_WRITER_TOOLSETS = ["file", "terminal", "vision"]


def resolve_toolsets(names):
    """toolset 名列表 → 去重展开的 schema 列表(对齐 hermes)。未知名忽略并打 warning。"""
    seen, out = set(), []
    for n in (names or []):
        ts = TOOLSETS.get(n)
        if ts is None:
            print(f"[resolve_toolsets] 未知 toolset:{n!r},忽略", flush=True)
            continue
        for tool in ts:
            if tool not in seen:
                seen.add(tool)
                out.append(SCHEMAS[tool])
    return out


def orchestrator_tools():
    """编排器的工具 schema 列表(Anthropic 格式)。"""
    return resolve_toolsets(ORCHESTRATOR_TOOLSETS)


def subagent_tools():
    """slide-writer 子 agent 的默认工具(兼容旧的 _build_child 路径)。"""
    return resolve_toolsets(SLIDE_WRITER_TOOLSETS)


def dispatch(agent, name, args):
    """按名字找工具——先查编排器专属的 extra_tools(如 delegate_task),再查 BUILTINS——
    用 agent 上下文执行。返回字符串,或图像结果 dict({image_b64, media_type, path, summary})。"""
    if name in agent.extra_tools:
        return agent.extra_tools[name](**args)
    fn = BUILTINS.get(name)
    if not fn:
        return f"未知工具 {name}"
    return fn(agent, **args)
