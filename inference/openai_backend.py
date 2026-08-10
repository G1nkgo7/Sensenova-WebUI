#!/usr/bin/env python3
"""OpenAI(chat.completions)后端适配 —— 让 agent_loop 的整套流程能驱动**部署的 student 模型**
(vLLM,OpenAI 兼容),而不改动 run_loop / 工具 / 渲染 / 验收。

用法:Agent.__init__ 里当 MODEL_BACKEND=openai 时,把 self.client 换成本模块的 OpenAIShim;
它暴露与 anthropic 一样的 `.messages.create(**kwargs)`,内部把 anthropic 风格的
(system, messages-blocks, tools-input_schema)翻成 OpenAI chat body,POST 到端点,再把回复
翻回 anthropic 风格的「响应对象」(.content=block 列表,.stop_reason),让上层代码原样工作。

模型是在 to_openai.py 产出的 OpenAI 格式数据上训练的,所以这里的翻译正是其**逆向**。
"""
import json
import os
import re
import urllib.request
import urllib.error
from types import SimpleNamespace

# 兜底:模型有时把工具调用当文本吐(Hermes 格式),还常落在 reasoning 里,vLLM 漏解析成
# 结构化 tool_calls。这里把这种文本救回成 (name, args)。容忍缺失的闭合标签(模型常截断)。
_RE_FUNC = re.compile(r"<function=([A-Za-z_]\w*)\s*>(.*?)(?=<function=|</tool_call>|$)", re.DOTALL)
_RE_PARAM = re.compile(r"<parameter=([A-Za-z_]\w*)\s*>\s*\n?(.*?)(?:\n?\s*</parameter>|(?=<parameter=)|(?=</tool_call>)|$)", re.DOTALL)
_inline_n = [0]


def _coerce(v):
    s = v.strip()
    if re.fullmatch(r"-?\d+", s):
        try: return int(s)
        except Exception: return s
    return s


def parse_inline_toolcalls(text):
    """从文本里抽 <function=NAME><parameter=KEY>VAL</parameter>… → [(name, args_dict)]。"""
    if not text or "<function=" not in text:
        return []
    out = []
    for m in _RE_FUNC.finditer(text):
        name, body = m.group(1), m.group(2)
        args = {pm.group(1): _coerce(pm.group(2)) for pm in _RE_PARAM.finditer(body)}
        out.append((name, args))
    return out


# ----------------------- 请求:anthropic → openai -----------------------
def _img_to_openai(block):
    """anthropic image 块 {type:image, source:{base64,media_type,data}} → openai image_url。
    live loop 里图片是真 base64(clean() 只在写盘时换成 shot)。"""
    src = block.get("source", {})
    if src.get("type") == "base64":
        media = src.get("media_type", "image/png")
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{src.get('data','')}"}}
    # 退化:写盘后的 {shot} 形态(理论上 live loop 不会遇到)
    return {"type": "text", "text": "<image>"}


def _toolresult_content(cc):
    """tool_result 的 content(str 或 [text/image 块])→ openai tool message 的 content。
    含图就用多模态数组;纯文本就给字符串。"""
    if isinstance(cc, str):
        return cc
    if not isinstance(cc, list):
        return "" if cc is None else str(cc)
    parts, has_img = [], False
    for y in cc:
        if not isinstance(y, dict):
            parts.append({"type": "text", "text": str(y)})
        elif y.get("type") == "text":
            parts.append({"type": "text", "text": y.get("text", "")})
        elif y.get("type") == "image":
            parts.append(_img_to_openai(y)); has_img = True
    if not has_img:
        return "\n".join(p["text"] for p in parts if p.get("text"))
    return parts


def to_openai_messages(system, messages):
    out = []
    if isinstance(system, list):
        # anthropic 风格 system blocks(agent_loop 为 prompt cache 挂了 cache_control)
        # → 拍平成纯文本;cache_control 是 anthropic 专属,漏给严格 openai 端点会 400。
        system = "\n".join(b.get("text", "") for b in system
                           if isinstance(b, dict) and b.get("type") == "text")
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        if role == "assistant":
            texts, calls = [], []
            for b in content:
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text", ""))
                elif t == "tool_use":
                    calls.append({"id": b.get("id"), "type": "function",
                                  "function": {"name": b.get("name"),
                                               "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}})
                # thinking 块:输入侧不回灌
            msg = {"role": "assistant", "content": ("\n".join(t for t in texts if t) or None)}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user:可能是 tool_result 列表,也可能是普通文本/图
            extra = []
            for b in content:
                if b.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                                "content": _toolresult_content(b.get("content"))})
                elif b.get("type") == "text":
                    extra.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "image":
                    extra.append(_img_to_openai(b))
            if extra:
                out.append({"role": "user", "content": extra})
    return out


def to_openai_tools(anthropic_tools):
    return [{"type": "function", "function": {
        "name": t.get("name"), "description": t.get("description", ""),
        "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
        for t in (anthropic_tools or [])]


# ----------------------- 响应:openai → anthropic 风格 -----------------------
_STOP = {"length": "max_tokens", "tool_calls": "tool_use", "stop": "end_turn"}


def _resp_to_blocks(msg):
    blocks = []
    rsn = msg.get("reasoning") or msg.get("reasoning_content") or ""
    txt = msg.get("content") or ""
    structured = msg.get("tool_calls") or []
    if rsn and rsn.strip():
        blocks.append(SimpleNamespace(type="thinking", thinking=rsn, signature=None))
    # text 块:若工具调用是以 <tool_call> 文本泄漏进 content 的,从 text 里剔掉,避免污染
    txt_clean = re.sub(r"<tool_call>.*?</tool_call>", "", txt, flags=re.DOTALL)
    txt_clean = re.sub(r"<function=.*", "", txt_clean, flags=re.DOTALL).strip()
    if txt_clean:
        blocks.append(SimpleNamespace(type="text", text=txt_clean))
    for tc in structured:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        blocks.append(SimpleNamespace(type="tool_use", id=tc.get("id"),
                                      name=fn.get("name"), input=args))
    # 兜底:没有结构化 tool_calls 时,从 reasoning + content 里救文本泄漏的工具调用
    if not structured:
        for name, args in (parse_inline_toolcalls(rsn) + parse_inline_toolcalls(txt)):
            _inline_n[0] += 1
            blocks.append(SimpleNamespace(type="tool_use", id=f"inline_{_inline_n[0]}",
                                          name=name, input=args))
    return blocks


# ----------------------- 客户端 shim -----------------------
class _Messages:
    def __init__(self, shim):
        self.shim = shim

    def create(self, model=None, max_tokens=2048, system="", messages=None, tools=None, **kw):
        body = {
            "model": self.shim.model or model,
            "max_tokens": max_tokens,
            "messages": to_openai_messages(system, messages or []),
            # 服务端 checkpoint 无 generation_config.json，vLLM 兜底是 T=1.0 裸采样；
            # Web Demo 静态链路在客户端固定为较低随机性采样。
            "temperature": float(os.environ.get("STUDENT_TEMPERATURE", "0.3")),
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        if os.environ.get("STUDIO_THINKING_TRANSPORT") == "chat_template_kwargs":
            body["chat_template_kwargs"] = {
                "enable_thinking": os.environ.get(
                    "STUDIO_EFFECTIVE_THINKING",
                    os.environ.get("STUDIO_ENABLE_THINKING", "0"),
                ) == "1"
            }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.shim.base.rstrip("/") + "/chat/completions",
                                     data=data, headers=self.shim.headers, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=self.shim.timeout)
            d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"openai backend HTTP {e.code}: {e.read()[:300]}")
        msg = d["choices"][0]["message"]
        fr = d["choices"][0].get("finish_reason", "stop")
        u = d.get("usage") or {}
        usage = SimpleNamespace(input_tokens=u.get("prompt_tokens", 0),
                                output_tokens=u.get("completion_tokens", 0),
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(content=_resp_to_blocks(msg),
                               stop_reason=_STOP.get(fr, "end_turn"), usage=usage)

    def stream(self, **kwargs):
        """鸭子化 anthropic 的 messages.stream(**kw):V2 core/agent.py 用
        `with client.messages.stream(**kw) as s: s.get_final_message()` 驱动。
        我们是 urllib 阻塞式、无 Anthropic SDK 的 >10min 非流式守卫,一发一收即可(复用 create)。"""
        return _PseudoStream(self, kwargs)


class _PseudoStream:
    """伪流式上下文管理器:鸭子化 anthropic MessageStream。__enter__ 返回自身,
    get_final_message() 即阻塞式 create()(同型返回 content/stop_reason/usage)。"""
    def __init__(self, messages, kwargs):
        self._messages = messages
        self._kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._messages.create(**self._kwargs)


class OpenAIShim:
    """鸭子类型成 anthropic.Anthropic:只用到 .messages.create(...)。"""
    def __init__(self, base, model, key="EMPTY", timeout=600):
        self.base = base
        self.model = model
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json",
                        "Authorization": f"Bearer {key or 'EMPTY'}"}
        self.messages = _Messages(self)
