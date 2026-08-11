"""Optional Nova exact-raw transport for the sn-ppt-web harness.

The Presenter Skill and role workflow remain unchanged.  This module only
replaces the two model transports required by the Nova V2 brushing contract:
the main Anthropic call and the text-producing ``vision_analyze`` auxiliary
call.  It deliberately reuses the repository's audited generic raw recorder
instead of defining a second raw format.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import anthropic
import httpx


_module_lock = threading.Lock()
_recorder_module = None
_client_local = threading.local()


def enabled() -> bool:
    return os.environ.get("NOVA_RAW_V2", "0") == "1"


def _load_recorder_module():
    global _recorder_module
    with _module_lock:
        if _recorder_module is not None:
            return _recorder_module
        path = Path(
            os.environ.get(
                "NOVA_RAW_RECORDER_MODULE",
                "/mnt/afs/hejiatong/multimodal_design/"
                "ppt-html-pipeline/core/nova_raw.py",
            )
        )
        if not path.is_file():
            raise RuntimeError(f"Nova raw recorder module is missing: {path}")
        spec = importlib.util.spec_from_file_location(
            "long_horizon_presenter_nova_raw", path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load Nova raw recorder: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _recorder_module = module
        return module


def recorder_source() -> dict[str, str]:
    module = _load_recorder_module()
    path = Path(module.__file__).resolve()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def create_recorder(agent):
    if not enabled():
        return None
    raw_root = str(os.environ.get("NOVA_RAW_ROOT") or "").strip()
    run_id = str(os.environ.get("NOVA_RUN_ID") or "").strip()
    if not raw_root or not run_id:
        raise RuntimeError("NOVA_RAW_ROOT and NOVA_RUN_ID are required")
    module = _load_recorder_module()
    return module.NovaRawRecorder(
        root=Path(raw_root),
        run_id=run_id,
        sample_id=agent.sid,
        role=agent.role,
        label=agent.label,
        tools=agent.tools,
        initial_user=agent.initial_user,
    )


def _aux_client() -> anthropic.Anthropic:
    base_url = str(os.environ.get("NOVA_VISION_PROXY_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("NOVA_VISION_PROXY_BASE_URL is required")
    client = getattr(_client_local, "aux_client", None)
    client_base = getattr(_client_local, "aux_client_base", None)
    if client is None or client_base != base_url:
        timeout = float(os.environ.get("NOVA_REQUEST_TIMEOUT", "1800"))
        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "nova-local-proxy"),
            base_url=base_url,
            http_client=httpx.Client(trust_env=False, timeout=timeout),
        )
        _client_local.aux_client = client
        _client_local.aux_client_base = base_url
    return client


def _stop_sequences() -> list[str]:
    raw = os.environ.get("NOVA_AGENT_STOP_SEQUENCES_JSON", "")
    if not raw:
        return []
    value = json.loads(raw)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise RuntimeError("NOVA_AGENT_STOP_SEQUENCES_JSON must be a string list")
    return value


def _response_bytes(raw_response) -> bytes:
    value = raw_response.content
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _record_transport_failure(
    recorder,
    *,
    request_kind: str,
    invocation_id: str,
    attempt_id: str,
    request_id: str,
    parent_tool_use_id: str,
    request_payload: dict[str, Any],
    error: Exception,
) -> None:
    response = getattr(error, "response", None)
    content = getattr(response, "content", b"") or b""
    response_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    parsed = None
    if response_bytes:
        try:
            candidate = json.loads(response_bytes)
            parsed = candidate if isinstance(candidate, dict) else None
        except (TypeError, ValueError):
            pass
    recorder.stage_response(
        request_kind=request_kind,
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        parent_tool_use_id=parent_tool_use_id,
        response_bytes=response_bytes,
        response_payload=parsed,
        error=f"{type(error).__name__}: {str(error)[:500]}",
    )
    recorder.record_attempt(
        request_kind=request_kind,
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        request_id=request_id,
        parent_tool_use_id=parent_tool_use_id,
        request_payload=request_payload,
        response_bytes=response_bytes,
        response_payload=parsed,
        selected=False,
        status="http_error" if response_bytes else "transport_error",
        error=f"{type(error).__name__}: {str(error)[:500]}",
        staged=True,
        http_status=getattr(response, "status_code", None),
    )


def _call_exact_raw(
    agent,
    *,
    client,
    kwargs: dict[str, Any],
    request_kind: str,
    parent_tool_use_id: str = "",
):
    recorder = agent.nova_raw
    invocation_id = recorder.new_invocation_id(request_kind)
    max_retries = int(os.environ.get("NOVA_TRANSPORT_RETRIES", "6"))
    timeout = float(os.environ.get("NOVA_REQUEST_TIMEOUT", "1800"))
    for ordinal in range(1, max_retries + 1):
        attempt_id = f"attempt-{ordinal:03d}-{uuid.uuid4().hex}"
        request_id = f"request-{uuid.uuid4().hex}"
        context = recorder.context(
            request_kind=request_kind,
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            request_id=request_id,
            parent_tool_use_id=parent_tool_use_id,
        )
        call_kwargs = dict(kwargs)
        extra_body = dict(call_kwargs.pop("extra_body", {}) or {})
        extra_body.update(
            {
                "nova_include_trace": True,
                "nova_trace_context": context,
            }
        )
        call_kwargs.update(
            {
                "stream": False,
                "extra_body": extra_body,
                "timeout": timeout,
            }
        )
        stops = _stop_sequences()
        if stops:
            call_kwargs["stop_sequences"] = stops
        request_payload = {
            key: value
            for key, value in call_kwargs.items()
            if key not in {"timeout", "extra_body"}
        }
        request_payload.update(extra_body)
        recorder.begin_attempt(
            request_kind=request_kind,
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            parent_tool_use_id=parent_tool_use_id,
            request_payload=request_payload,
        )
        try:
            raw = client.messages.with_raw_response.create(**call_kwargs)
            response_bytes = _response_bytes(raw)
            try:
                response_payload = json.loads(response_bytes)
            except (TypeError, ValueError) as exc:
                response_payload = None
                recorder.stage_response(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    parent_tool_use_id=parent_tool_use_id,
                    response_bytes=response_bytes,
                    response_payload=None,
                    error=f"{type(exc).__name__}: {exc}",
                )
                recorder.record_attempt(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    request_id=request_id,
                    parent_tool_use_id=parent_tool_use_id,
                    request_payload=request_payload,
                    response_bytes=response_bytes,
                    response_payload=None,
                    selected=False,
                    status="invalid_json",
                    error=f"{type(exc).__name__}: {exc}",
                    staged=True,
                    http_status=getattr(raw.http_response, "status_code", 200),
                )
                raise RuntimeError("Nova returned non-JSON") from exc
            recorder.stage_response(
                request_kind=request_kind,
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                parent_tool_use_id=parent_tool_use_id,
                response_bytes=response_bytes,
                response_payload=response_payload,
            )
            trace = (response_payload or {}).get("nova_internal_trace")
            trace_ok = (
                isinstance(trace, dict)
                and trace.get("schema_version") == "nova_agent.internal_model_calls.v1"
                and isinstance(trace.get("status"), dict)
                and trace["status"].get("ok") is True
            )
            if not trace_ok:
                recorder.record_attempt(
                    request_kind=request_kind,
                    invocation_id=invocation_id,
                    attempt_id=attempt_id,
                    request_id=request_id,
                    parent_tool_use_id=parent_tool_use_id,
                    request_payload=request_payload,
                    response_bytes=response_bytes,
                    response_payload=response_payload,
                    selected=False,
                    status="missing_or_invalid_nova_trace",
                    error="top-level nova_internal_trace is missing or invalid",
                    staged=True,
                    http_status=getattr(raw.http_response, "status_code", 200),
                )
                raise RuntimeError("missing or invalid Nova trace")
            response = raw.parse()
            recorder.record_attempt(
                request_kind=request_kind,
                invocation_id=invocation_id,
                attempt_id=attempt_id,
                request_id=request_id,
                parent_tool_use_id=parent_tool_use_id,
                request_payload=request_payload,
                response_bytes=response_bytes,
                response_payload=response_payload,
                selected=True,
                status="ok",
                staged=True,
                http_status=getattr(raw.http_response, "status_code", 200),
            )
            return response
        except Exception as exc:  # noqa: BLE001
            attempt_root = recorder._attempt_root(
                request_kind, invocation_id, attempt_id, parent_tool_use_id
            )
            if not (attempt_root / "attempt.json").is_file():
                if (attempt_root / "response.json").is_file():
                    recorder.record_attempt(
                        request_kind=request_kind,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        request_id=request_id,
                        parent_tool_use_id=parent_tool_use_id,
                        request_payload=request_payload,
                        response_bytes=locals().get("response_bytes", b""),
                        response_payload=locals().get("response_payload"),
                        selected=False,
                        status="sdk_parse_error",
                        error=f"{type(exc).__name__}: {str(exc)[:500]}",
                        staged=True,
                        http_status=getattr(
                            locals().get("raw", None), "http_response", None
                        ).status_code if getattr(
                            locals().get("raw", None), "http_response", None
                        ) is not None else None,
                    )
                else:
                    _record_transport_failure(
                        recorder,
                        request_kind=request_kind,
                        invocation_id=invocation_id,
                        attempt_id=attempt_id,
                        request_id=request_id,
                        parent_tool_use_id=parent_tool_use_id,
                        request_payload=request_payload,
                        error=exc,
                    )
            if ordinal >= max_retries or isinstance(
                exc,
                (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                 anthropic.BadRequestError),
            ):
                agent.log(f"[Nova {request_kind} failed] {type(exc).__name__}: {str(exc)[:180]}")
                return None
            wait = min(2 ** ordinal, 45)
            agent.log(
                f"[Nova {request_kind} retry {ordinal}/{max_retries}] "
                f"{type(exc).__name__}; {wait}s"
            )
            time.sleep(wait)
    return None


def call_main(agent, kwargs: dict[str, Any]):
    return _call_exact_raw(
        agent,
        client=agent.client,
        kwargs=kwargs,
        request_kind="hermes_main",
    )


def call_vision_auxiliary(
    agent,
    *,
    image_bytes: bytes,
    media_type: str,
    source_path: str,
    question: str,
    parent_tool_use_id: str,
) -> str:
    if not parent_tool_use_id:
        raise RuntimeError("Nova vision call is missing parent_tool_use_id")
    agent.nova_raw.add_asset(image_bytes, media_type, source_path)
    prompt = str(question or "").strip() or (
        "Inspect the image carefully. Report concrete layout, legibility, "
        "clipping, overlap, hierarchy, and visual defects."
    )
    kwargs: dict[str, Any] = {
        "model": agent.model,
        "max_tokens": int(os.environ.get("NOVA_VISION_MAX_TOKENS", "8192")),
        "system": (
            "You are the visual inspection auxiliary agent. Use the built-in "
            "vision_reader supplied by Nova, inspect the provided image, and "
            "answer the exact question with concise pixel-grounded evidence."
        ),
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": agent.think_effort},
    }
    response = _call_exact_raw(
        agent,
        client=_aux_client(),
        kwargs=kwargs,
        request_kind="vision_analyze_aux",
        parent_tool_use_id=parent_tool_use_id,
    )
    if response is None:
        raise RuntimeError("Nova vision auxiliary request failed")
    text = "\n".join(
        str(getattr(block, "text", "")).strip()
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
        and str(getattr(block, "text", "")).strip()
    ).strip()
    if not text:
        raise RuntimeError("Nova vision auxiliary returned empty text")
    return text


def finalize_agent(agent, messages: list[dict[str, Any]], finished_clean: bool):
    if not getattr(agent, "nova_raw", None):
        return None
    return agent.nova_raw.finalize(
        messages=messages,
        final_answer=agent.final_text,
        status="completed" if finished_clean else "rejected",
        exit_reason=str(agent.exit_reason or "unknown"),
        artifacts=[],
    )


def abort_agent(agent, error: Exception) -> None:
    recorder = getattr(agent, "nova_raw", None)
    if recorder is not None:
        recorder.abort(f"{type(error).__name__}: {str(error)[:1000]}")
