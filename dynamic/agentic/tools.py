"""工具实现 + schema（单 Agent 版，改造自同事 distillation/tools.py）。

与同事的差异：
- 删 delegate_task（单 agent）。
- 去 orchestrator/subagent 二分 → 单一 agent_tools()：
  read_file/write_file/edit/bash/vision_analyze/image_generate/web_search/web_fetch。
- bash 收紧：只用于跑 render_deck.py 与 ls/查看 assets；实现层黑名单拦网络/安装/危险命令
  （curl/wget 仍禁——联网只经受控的 web_search/web_fetch）。
- image_generate 受 config.ENABLE_IMAGE_GEN 控制（端点不可用时整组撤下）。
- web_search/web_fetch 受 config.ENABLE_WEB 控制（无 SERPER_API_KEY 时整组撤下，不做免 key
  降级搜索——训练轨迹里不要出现降级来源）。web_search 走 Serper（对齐 Hermes 的
  web_search/web_extract 两件套设计）；web_fetch 免 key。

工具是自由函数，第一个参数是 agent 上下文对象（提供 ws / safe / read_path / 生图端点 / 计数器）。
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import re
import shlex
import subprocess
import sys
from html import unescape

import requests

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# bash 黑名单：网络/安装/提权/破坏性命令一律拒绝（teacher 生成训练数据，杜绝越界副作用）。
_BASH_BLACKLIST = re.compile(
    r"\b(curl|wget|pip|pip3|conda|npm|yarn|apt|apt-get|yum|sudo|ssh|scp|rsync|nc|telnet|"
    r"systemctl|kill|pkill|reboot|shutdown|mkfs|dd|chmod\s+777|chown)\b"
    r"|rm\s+-rf\s+/|>\s*/etc|:\(\)\s*\{")
# bash 允许的首词（跑渲染脚本 / 看产物 / 轻量文件操作 / 只读检索）
# grep 是只读检索：模型在大 HTML 里定位编辑锚点、数元素、查类名时本能就用它，放行省掉每次先撞墙。
_BASH_ALLOW_FIRST = ("python", "python3", "ls", "cat", "head", "tail", "find", "echo", "mkdir", "wc", "grep")
# bash_relaxed（仅 studio 在线体验置位；teacher 造数据 agent 永不置位、行为不变）额外放行的首词：
# 在大单文件 HTML 上 edit 精确匹配很脆，放开 sed/awk/mv/cp/rm 等文件处理命令，配 cwd 限定 + 路径守卫
# 把 agent 关在当前对话工作区里，越界写/删一律拒绝。
_BASH_ALLOW_FIRST_RELAXED = _BASH_ALLOW_FIRST + (
    "sed", "awk", "mv", "cp", "rm", "touch", "sort", "uniq", "diff", "tr", "cut",
    "tee", "printf", "basename", "dirname", "pwd", "cd", "true", "test")
# relaxed 下做路径守卫时：以这些词开头的命令会写/删文件系统，其路径必须落在工作区内。
_BASH_WRITE_CMDS = {"mv", "cp", "rm", "touch", "tee", "mkdir", "sed", "awk"}
# 写类命令（按参数路径写/删文件系统的）。其余写出只能经重定向（单独拦）。
_BASH_WRITE_BY_ARG = {"mv", "cp", "rm", "touch", "mkdir", "tee", "rmdir", "ln", "install", "rsync"}
# 重定向目标（> >>，绝对或相对都抓；含 awk/sed 程序串里的 print > "x"，宁可过拦也不放过）。
_REDIR_RE = re.compile(r">>?\s*['\"]?([^\s'\";|&<>]+)")
# 命令分段：在 ; | & && || 与换行处切，逐段查首词是否写类命令。
_SEG_SPLIT_RE = re.compile(r"\|\||&&|[;|&\n]")


# =============================================================== 工具实现

def read_file(agent, path, offset=1, limit=500):
    """只读文本（图片用 vision_analyze）。输出带行号 'LINE_NUM|CONTENT'，用 offset/limit 分页。
    可读工作区文件与只读的 skills/ 树；目录则列条目（相当于 ls）。

    大文件防读不全：单次输出过长会在**行边界**截断，并附 '共 N 行 / 续读 offset=M' 提示——
    不会像旧实现那样静默砍断，让模型误以为已读到文件末尾。CAP 取 7800（略低于 agent_loop 对
    工具结果统一的 8000 截断），保证截断提示这一行本身不会再被外层 loop 砍掉。"""
    if os.path.splitext(path)[1].lower() in IMG_EXT:
        return f"read_file 错误：'{path}' 是图片，请用 vision_analyze 查看。"
    fp = agent.read_path(path)
    if os.path.isdir(fp):
        entries = sorted(os.listdir(fp))
        if not entries:
            return f"{path}/ (空目录)"
        listing = [e + ("/" if os.path.isdir(os.path.join(fp, e)) else "") for e in entries]
        return f"{path}/ 下的条目：\n" + "\n".join(listing)
    with open(fp, encoding="utf-8") as f:
        all_lines = f.read().splitlines()
    total = len(all_lines)
    start = max(1, int(offset or 1))
    lim = int(limit or 500)
    sel = all_lines[start - 1:start - 1 + lim]
    numbered = "\n".join(f"{start + i}|{ln}" for i, ln in enumerate(sel))
    CAP = 7800
    if len(numbered) > CAP:
        cut = numbered[:CAP].rsplit("\n", 1)[0]
        last = start + cut.count("\n")
        numbered = cut + f"\n\n[… 截断：已显示第 {start}–{last} 行（共 {total} 行）；续读 offset={last + 1}]"
    return numbered


def write_file(agent, path, content):
    fp = agent.safe(path)
    os.makedirs(os.path.dirname(fp) or fp, exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {len(content.encode())} 字节到 {path}"


def edit(agent, path, old_string, new_string, replace_all=False):
    fp = agent.safe(path)
    with open(fp, encoding="utf-8") as f:
        s = f.read()
    n = s.count(old_string)
    if n == 0:
        return f"edit 错误：在 {path} 里找不到 old_string"
    if n > 1 and not replace_all:
        return f"edit 错误：old_string 出现了 {n} 次（不唯一）；确认要全改请传 replace_all=true"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(s.replace(old_string, new_string))
    return f"已编辑 {path}"


def _bash_path_guard(agent, cmd):
    """relaxed 模式下把写/删类命令关在当前对话工作区内。返回 None 放行，或错误字符串。

    思路：任何"会落地到文件系统"的路径——重定向目标(> >>)、写类命令(mv/cp/rm/touch/mkdir/tee/ln…)
    的参数、cd 目标——一律相对 ws 解析后取 realpath，必须落在 ws 内，否则拒绝。relpath 解析+realpath
    自动覆盖三类逃逸：① 绝对路径越界 ② 相对 `..` 上跳 ③ 经 skills 等符号链接穿到训练目录。
    用 ;|&&|| 等分段、逐段查首词，堵住"管道/串联里的写命令"(echo x|tee /etc, ls;rm /x)。
    只读命令(cat/grep/find/sed 不带 -i/awk 不重定向…)不拦——它们顶多读到越界内容，不改文件系统。"""
    ws = os.path.realpath(agent.ws)

    def _inside(p):
        full = p if os.path.isabs(p) else os.path.join(ws, p)
        rp = os.path.realpath(full)
        return rp == ws or rp.startswith(ws + os.sep)

    def _toks(seg):
        try:
            return shlex.split(seg)
        except Exception:  # noqa: BLE001  引号不闭合等 → 退化按空白切
            return seg.split()

    # 1) 所有重定向目标（绝对/相对都抓；含 awk/sed 程序串里的 > "x"）必须在 ws 内
    for target in _REDIR_RE.findall(cmd):
        if not _inside(target):
            return f"bash 错误：禁止写到工作区外 '{target}'（只能在当前对话工作区内操作）。"

    # 2) 逐段：写类命令的每个路径参数、cd 目标，必须在 ws 内
    for seg in _SEG_SPLIT_RE.split(cmd):
        toks = _toks(seg)
        if not toks:
            continue
        c0 = os.path.basename(toks[0])
        if c0 == "cd":
            for t in toks[1:]:
                if not t.startswith("-") and not _inside(t):
                    return f"bash 错误：禁止 cd 到工作区外 '{t}'。"
            continue
        is_write = c0 in _BASH_WRITE_BY_ARG or (c0 in ("sed", "perl") and any(
            t == "-i" or t.startswith("-i") for t in toks[1:])) or (
            c0 == "find" and any(f in toks for f in ("-delete", "-exec", "-execdir", "-fprint", "-fprintf")))
        if not is_write:
            continue
        for t in toks[1:]:
            if t.startswith("-"):
                continue                       # 选项，不是路径
            looks_path = ("/" in t) or (".." in t) or os.path.exists(os.path.join(ws, t))
            if looks_path and not _inside(t):
                return (f"bash 错误：'{c0}' 不能操作工作区外的路径 '{t}'"
                        f"（只能在当前对话工作区内操作）。")
    return None


# 匹配命令里以 python / python3 起头(命令开头或 ; && | 之后)的调用；
# 仅用于 harness 执行层把跑 render_deck.py 的裸解释器换成 fancy-sft，不改模型可见文本。
_RENDER_PY_RE = re.compile(r'(^|[;&|]\s*)(python3?|py)\b')


def _pin_render_python(command: str) -> str:
    """把跑 render_deck.py 的裸 python/python3 改写为本进程解释器(fancy-sft)。
    只在命令确实调用 render_deck.py 时改写；不含 render_deck.py 的命令原样返回。
    改写的是执行字符串，模型看到与轨迹记录的原始 command 不受影响。

    根因:模型敲的裸 `python` 在 worker 环境里解析到系统 /usr/bin/python——它能从
    ~/.local import 到 playwright 却缺 chromium 依赖库(libnss3/libgbm/libX* 等只在
    fancy-sft conda + ~/pwdeps 里),导致浏览器启动即崩(TargetClosedError)。"""
    if "render_deck.py" not in command:
        return command
    exe = sys.executable  # worker 由 fancy-sft python 起，这里即 fancy-sft 解释器

    def _sub(m):
        return f"{m.group(1)}{shlex.quote(exe)}"

    return _RENDER_PY_RE.sub(_sub, command)


def bash(agent, command, timeout=None):
    """在工作区目录下执行 shell 命令。
    默认（teacher 造数据）：仅 render_deck.py / ls / 查看 assets / 只读检索。
    relaxed（仅 studio 在线体验，agent.bash_relaxed=True）：放开 sed/awk/mv/cp/rm 等文件处理命令，
    配 cwd 限定 + 路径守卫把操作关在当前对话工作区内。"""
    if not isinstance(command, str) or not command.strip():
        return "bash 错误：command 不能为空"
    cmd = command.strip()
    relaxed = bool(getattr(agent, "bash_relaxed", False))
    if _BASH_BLACKLIST.search(cmd):
        return ("bash 错误：命令含被禁止的操作（网络/安装/提权/破坏性）。bash 只用于跑 "
                "render_deck.py 渲染、ls/查看 assets。")
    allow = _BASH_ALLOW_FIRST_RELAXED if relaxed else _BASH_ALLOW_FIRST
    first = cmd.split()[0] if cmd.split() else ""
    if first not in allow:
        if relaxed:
            return (f"bash 错误：不允许的命令 '{first}'。可用：{', '.join(allow)}。"
                    f"仅限在当前对话工作区内操作。")
        return (f"bash 错误：不允许的命令 '{first}'。bash 仅用于：渲染 "
                f"`python {agent.render_script} deck.html shots/ --page N|--all`、ls/查看 assets。")
    if relaxed:
        guard_err = _bash_path_guard(agent, cmd)
        if guard_err:
            return guard_err
    to = int(timeout) if timeout else agent.bash_timeout
    # harness 修复(对模型不可见)：执行前把 render_deck 命令的前导 python/python3 改写为
    # 本进程解释器(fancy-sft)。只改传给 subprocess 的字符串，模型看到/记录的原始 command 不变。
    exec_command = _pin_render_python(command)
    try:
        r = subprocess.run(exec_command, shell=True, cwd=agent.ws,
                           capture_output=True, text=True, timeout=to)
    except subprocess.TimeoutExpired:
        return f"bash 错误：命令超过 {to}s 超时"
    except Exception as e:  # noqa: BLE001
        return f"bash 错误：{e}"
    out = (r.stdout or "")
    if r.stderr:
        out += ("\n[stderr] " + r.stderr)
    out = out.strip()
    return out[:8000] if out else f"(命令完成，退出码 {r.returncode}，无输出)"


def vision_analyze(agent, path, prompt=None):
    """返回图片像素让模型看见截图。降采样到最长边 MAX_VISION_EDGE，绕开服务端尺寸/体积限制。"""
    fp = agent.read_path(path)
    if not os.path.exists(fp) or os.path.isdir(fp):
        return f"vision_analyze 错误：没有这张图 {path}"
    try:
        import io

        from PIL import Image
        with Image.open(fp) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = agent.max_vision_edge / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
    except ImportError:
        with open(fp, "rb") as f:
            data = f.read()
    except Exception as e:  # noqa: BLE001
        return (f"vision_analyze 错误：{path} 不是可解析的图片（{type(e).__name__}）。"
                f"只能看 PNG/JPG；deck 页请先渲染："
                f"python {agent.render_script} deck.html shots/ --page N")
    return {"image_b64": base64.b64encode(data).decode(), "media_type": "image/png",
            "path": os.path.relpath(fp, agent.ws), "summary": f"正在查看 {path}。"}


def image_generate(agent, prompt, aspect_ratio="16:9", out_path=None):
    """生成照片/插画类配图（不用于图表），存到 assets/，返回相对路径。"""
    if not getattr(agent, "enable_image_gen", False):
        return "image_generate 不可用（本批次未启用生图）。一切视觉请用 SVG/Canvas/CSS 代码绘制。"
    size = {"16:9": "1536x1024", "9:16": "1024x1536", "4:3": "1536x1024",
            "3:4": "1024x1536", "1:1": "1024x1024"}.get(aspect_ratio, "1536x1024")
    try:
        d = requests.post(f"{agent.img_base}/images/generations",
                          headers={"Authorization": f"Bearer {agent.img_key}",
                                   "Content-Type": "application/json"},
                          json={"model": agent.image_model, "prompt": prompt, "size": size, "n": 1},
                          timeout=180).json()
    except Exception as e:  # noqa: BLE001
        return f"image_generate 错误：{e}"
    if "data" not in d or not d["data"]:
        return f"image_generate 错误：{json.dumps(d, ensure_ascii=False)[:200]}"
    it = d["data"][0]
    if it.get("b64_json"):
        data = base64.b64decode(it["b64_json"])
    elif it.get("url"):
        try:
            data = requests.get(it["url"], timeout=120).content
        except Exception as e:  # noqa: BLE001
            return f"image_generate 错误：下载图片失败 {e}"
    else:
        return "image_generate 错误：没有返回图片"
    rel = None
    if out_path:
        name = os.path.basename(str(out_path)).strip()
        if name:
            if not name.lower().endswith(".png"):
                name += ".png"
            rel = f"assets/{name}"
    if rel is None:
        agent.img_n += 1
        rel = f"assets/img_{agent.img_n:02d}.png"
    os.makedirs(agent.safe("assets"), exist_ok=True)
    with open(agent.safe(rel), "wb") as f:
        f.write(data)
    return rel


def web_search(agent, query, limit=5):
    """Serper（Google）网页搜索，返回 title/url/source/snippet 的 JSON 列表。
    无 key 时本工具不会被挂载（config.ENABLE_WEB），这里的报错只是兜底。"""
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        return "web_search 不可用（未配置 SERPER_API_KEY）。请基于你已可靠掌握的知识撰写内容。"
    if not isinstance(query, str) or not query.strip():
        return "web_search 错误：query 不能为空"
    try:
        n = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        n = 5
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query.strip(), "num": n},
            timeout=45)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return f"web_search 错误：{type(e).__name__}: {e}"
    rows = [{"title": it.get("title"), "url": it.get("link"),
             "source": it.get("source"), "snippet": it.get("snippet")}
            for it in data.get("organic", [])[:n]]
    # answerBox 常直接给出数字/日期类事实，有就带上（放最前，模型少 fetch 一次）。
    box = data.get("answerBox") or {}
    if box.get("answer") or box.get("snippet"):
        rows.insert(0, {"title": box.get("title"), "url": box.get("link"),
                        "source": "answerBox", "snippet": box.get("answer") or box.get("snippet")})
    if not rows:
        return f"web_search：'{query}' 没有搜到结果。换个关键词重试，或如实标注查证不到。"
    return json.dumps(rows, ensure_ascii=False, indent=2)


def web_fetch(agent, url, char_limit=7000):
    """抓取一个公开网页并抽出正文纯文本（剥 script/style/标签、压空白）。
    默认 7000 字符：agent_loop 对工具结果统一 8000 截断，取略低值保证截断提示可见。"""
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        return "web_fetch 错误：url 必须是 http(s) 链接（来自 web_search 结果或 query 本身）"
    try:
        cap = max(1000, min(int(char_limit or 7000), 7000))
    except (TypeError, ValueError):
        cap = 7000
    try:
        resp = requests.get(
            url.strip(),
            headers={"User-Agent": "Mozilla/5.0 (compatible; DazzleDeckResearch/1.0)"},
            timeout=45)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"web_fetch 错误：{type(e).__name__}: {e}"
    ctype = resp.headers.get("content-type", "")
    if ctype and ("html" not in ctype and "text" not in ctype and "json" not in ctype and "xml" not in ctype):
        return f"web_fetch 错误：'{url}' 不是文本网页（content-type: {ctype}）。本工具只读正文，不下载文件/图片。"
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", resp.text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(re.sub(r"\s+", " ", text)).strip()
    if not text:
        return f"web_fetch：'{url}' 没有抽到正文文本（可能是纯 JS 渲染页）。换 web_search 结果里的其他来源。"
    if len(text) > cap:
        return text[:cap] + f"\n\n[… 截断：正文共约 {len(text)} 字符，已显示前 {cap}；关键信息通常在前部，如需更多换更具体的来源页]"
    return text


# =============================================================== 工具 schema

_READ = {
    "name": "read_file",
    "description": ("读文件内容（只读文本；图片用 vision_analyze）。输出带行号 'LINE_NUM|CONTENT'，"
                    "用 offset/limit 分页（默认从第 1 行起、最多 500 行）。可读工作区文件，也可读 "
                    "skills/dazzle-deck/ 下的 SKILL.md 与 references。路径是目录时返回条目清单（相当于 ls，"
                    "可查看 assets/）。单次输出过长会在行边界截断并提示总行数与续读 offset——"
                    "读大文件（如填了很多页的 deck.html）务必按提示用 offset 续读到读全，"
                    "不要以为第一次就读到了文件末尾。"),
    "input_schema": {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "文件或目录路径（相对工作区）"},
        "offset": {"type": "number", "description": "起始行(1 开始，默认 1)", "default": 1},
        "limit": {"type": "number", "description": "最多读多少行(默认 500)", "default": 500}}},
}
_WRITE = {
    "name": "write_file",
    "description": "把内容写入文件；不存在则创建，存在则覆盖，自动建父目录。用于写 plan.md 与 deck.html 骨架。",
    "input_schema": {"type": "object", "required": ["path", "content"], "properties": {
        "path": {"type": "string", "description": "文件路径(相对工作区)"},
        "content": {"type": "string", "description": "要写入的内容"}}},
}
_EDIT = {
    "name": "edit",
    "description": "把文件里一段精确字符串替换为新字符串。old_string 必须精确匹配且唯一，除非 replace_all=true。逐页填充 deck.html 的主力工具。",
    "input_schema": {"type": "object", "required": ["path", "old_string", "new_string"], "properties": {
        "path": {"type": "string", "description": "要编辑的文件路径"},
        "old_string": {"type": "string", "description": "要替换的精确文本"},
        "new_string": {"type": "string", "description": "替换后的文本"},
        "replace_all": {"type": "boolean", "description": "是否替换全部出现(默认 false)"}}},
}
_BASH = {
    "name": "bash",
    "description": ("在工作区目录下执行命令。**仅用于渲染 deck 与查看产物**：\n"
                    "  python skills/dazzle-deck/scripts/render_deck.py deck.html shots/ --page N   # 渲染第 N 页\n"
                    "  python skills/dazzle-deck/scripts/render_deck.py deck.html shots/ --all     # 全部页 + contact_sheet\n"
                    "成功时 stdout 末行是 PNG 路径，告警以 [console]/[static]/[blank]/[nav] 前缀打在前面；"
                    "渲染元数据写 shots/render.json。渲染后用 vision_analyze 看 PNG。也可 ls 查看 assets/。"
                    "禁止网络/安装/危险命令。"),
    "input_schema": {"type": "object", "required": ["command"], "properties": {
        "command": {"type": "string", "description": "shell 命令（限渲染脚本 / ls / 查看 assets）"}}},
}
_VISION = {
    "name": "vision_analyze",
    "description": "查看一张图片（render 产出的页截图或 contact_sheet）：返回像素让你看见它，检查溢出/遮挡/占位/破图/豆腐块/对比/入场动画是否到最终态。先渲后看，只读代码不算自检。",
    "input_schema": {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "图片路径，如 shots/page_03.png 或 shots/contact_sheet.png"},
        "prompt": {"type": "string", "description": "可选：想重点看什么"}}},
}
_WEB_SEARCH = {
    "name": "web_search",
    "description": ("搜索网页（Google）。用于查证你不可靠掌握的外部事实——真实数据、近期事件、"
                    "具体人物/产品/机构的参数与时间线；你已可靠掌握的知识不必搜。"
                    "返回 title/url/snippet 的 JSON 列表；采信的事实要连同来源 URL 记入 plan.md。"
                    "结果只用于文本内容事实：**网页里的图片 URL 一律不得写入 deck**（图片纪律见 skill）。"),
    "input_schema": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "搜索关键词（查具体事实而非泛搜；用该事实最可能出现的语言）"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                  "description": "返回条数（默认 5）", "default": 5}}},
}
_WEB_FETCH = {
    "name": "web_fetch",
    "description": ("抓取一个公开网页的正文纯文本（已剥标签），用于核验 web_search 结果里拿不准的关键来源，"
                    "或读取 query 中给出的 URL。信息够用即停，不要重复抓取同类页面。"),
    "input_schema": {"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string", "description": "http(s) 网页链接"},
        "char_limit": {"type": "integer", "minimum": 1000, "maximum": 7000,
                       "description": "正文最多返回的字符数（默认 7000）", "default": 7000}}},
}
_IMAGE_GEN = {
    "name": "image_generate",
    "description": ("生成照片/插画类配图（实物/地点/风光/食物/文物/人物场景等代码画不出的真实素材）。"
                    "**严禁用于图表/数据可视化/抽象概念/几何装饰/图标/UI**——这些用 SVG/Canvas/CSS 画。"
                    "prompt 必须带本 deck 的色板词（如 'deep navy and muted gold palette, low saturation, "
                    "editorial photography, no text, no watermark'）。返回 assets/ 下的路径。"),
    "input_schema": {"type": "object", "required": ["prompt"], "properties": {
        "prompt": {"type": "string", "description": "生成描述（务必含色板/质感/no text）"},
        "aspect_ratio": {"type": "string", "enum": ["16:9", "4:3", "1:1", "3:4", "9:16"],
                         "description": "宽高比，默认 16:9"},
        "out_path": {"type": "string", "description": "可选保存文件名，如 assets/hero.png"}}},
}

BUILTINS = {
    "read_file": read_file, "write_file": write_file, "edit": edit, "bash": bash,
    "vision_analyze": vision_analyze, "image_generate": image_generate,
    "web_search": web_search, "web_fetch": web_fetch,
}
_ALL_SCHEMAS = {s["name"]: s for s in (
    _READ, _WRITE, _EDIT, _BASH, _VISION, _WEB_SEARCH, _WEB_FETCH, _IMAGE_GEN)}


def agent_tools(enable_image_gen: bool = True, enable_web: bool = True) -> list[dict]:
    """单 agent 的工具 schema 列表（Anthropic 格式）。
    生图关闭时撤下 image_generate；联网关闭（含无 SERPER_API_KEY）时撤下 web_search/web_fetch。"""
    names = ["read_file", "write_file", "edit", "bash", "vision_analyze"]
    if enable_web:
        names += ["web_search", "web_fetch"]
    if enable_image_gen:
        names.append("image_generate")
    return [_ALL_SCHEMAS[n] for n in names]


def dispatch(agent, name, args):
    fn = BUILTINS.get(name)
    if not fn:
        return f"未知工具 {name}"
    if not isinstance(args, dict):
        return f"{name} 错误：参数必须是 JSON 对象"
    # 参数校验：缺必填→友好提示而非崩溃；未知参数名（如把 prompt 拼成 prameter）→ 忽略并提示，不崩。
    params = list(inspect.signature(fn).parameters.values())[1:]   # 跳过第一个 agent
    valid = {p.name for p in params}
    required = [p.name for p in params if p.default is inspect.Parameter.empty]
    missing = [r for r in required if r not in args]
    if missing:
        return (f"{name} 错误：缺少必填参数 {missing}。该工具参数：{sorted(valid)}。补齐后重试。")
    unknown = [k for k in args if k not in valid]
    clean = {k: v for k, v in args.items() if k in valid}
    try:
        res = fn(agent, **clean)
    except Exception as e:  # noqa: BLE001
        return f"{name} 错误：{type(e).__name__}: {e}"
    if unknown and isinstance(res, str):
        return f"{res}（忽略了未知参数 {unknown}，请检查参数名）"
    return res
