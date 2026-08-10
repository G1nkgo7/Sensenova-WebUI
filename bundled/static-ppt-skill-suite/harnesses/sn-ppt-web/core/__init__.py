"""core —— 通用 agent 运行时(agent / tools / trace)。零领域知识。"""
import json
import re


_CONTRACT_FIELD_RE = re.compile(
    r"(?i)^\s*(?:\*\*|__)?`?([a-z][a-z0-9_-]*)`?(?:\*\*|__)?"
    r"\s*[:：]\s*(.+?)\s*$"
)
_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.I | re.S
)
_LOCALIZED_STATUS_RE = re.compile(
    r"(?i)^(?:\*\*|__)?(?:status|状态)(?:\*\*|__)?\s*[:：]\s*"
    r"(ready|blocked|partial)(?:\*\*|__)?"
    r"(?:\s*(?:[,，。;；—-].*)?)?$"
)


def _strip_markdown_wrapper(value):
    """Remove only line-level Markdown wrappers, never prose around a verdict."""
    value = str(value or "").strip()
    while len(value) >= 2 and (
        (value.startswith("**") and value.endswith("**"))
        or (value.startswith("__") and value.endswith("__"))
        or (value.startswith("`") and value.endswith("`"))
    ):
        width = 2 if value[:2] in {"**", "__"} else 1
        value = value[width:-width].strip()
    return value


def _contract_value(key, value):
    """Normalize one explicit field without guessing a verdict from prose."""
    normalized_key = str(key).lower().replace("-", "_")
    # Some Review models serialize "no unresolved issues" as an empty JSON
    # array even though the compact acceptance contract uses the scalar
    # ``remaining: none``.  Treat only the empty array as equivalent.  Preserve
    # non-empty arrays as a non-``none`` scalar so the quality gate still fails.
    if normalized_key == "remaining" and isinstance(value, list):
        if not value:
            return "none"
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cleaned = _strip_markdown_wrapper(value).strip().lower()
    if normalized_key == "remaining" and cleaned == "[]":
        return "none"
    if normalized_key == "status":
        # Weak models often append a sentence after an otherwise explicit
        # ``status: ready`` line.  Preserve the verdict token and discard only
        # punctuation-delimited commentary on that same structured line.
        match = re.match(
            r"^(ready|blocked|partial)(?=$|\s|`|\*\*|__|[,，。;；—-])",
            cleaned,
        )
        if match:
            return match.group(1)
    return cleaned


def _final_contract(text):
    """Keep only explicit structured role verdicts from a child's final text.

    Models commonly emit the same contract as plain Markdown, bold Markdown or a
    fenced JSON object.  Accept those serialization variants, but never infer a
    verdict from prose such as ``Review 已返回 ready``.
    """
    fields = {}
    source = str(text or "")

    # Valid JSON is the least ambiguous representation.  Only scalar top-level
    # values belong to the compact handoff contract; nested evidence remains in
    # the persisted final response.
    json_blocks = [match.group(1) for match in _FENCED_JSON_RE.finditer(source)]
    stripped = source.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        json_blocks.append(stripped)
    for block in json_blocks:
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if not (
                isinstance(key, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key)
            ):
                continue
            normalized_key = key.lower().replace("-", "_")
            is_scalar = isinstance(value, (str, int, float, bool))
            is_remaining_list = normalized_key == "remaining" and isinstance(value, list)
            if is_scalar or is_remaining_list:
                fields[normalized_key] = _contract_value(normalized_key, value)

    for line in source.splitlines():
        # A bullet must contain following whitespace.  This avoids mistaking the
        # first asterisk of ``**status: ready**`` for a list marker.
        candidate = re.sub(r"^\s*[-*+]\s+", "", line.strip())
        candidate = _strip_markdown_wrapper(candidate)
        match = _CONTRACT_FIELD_RE.fullmatch(candidate)
        if not match:
            continue
        key, value = match.groups()
        normalized_key = key.lower().replace("-", "_")
        fields[normalized_key] = _contract_value(normalized_key, value)
    # A localized Review may correctly emit one standalone Chinese status line.
    # Accept that exact line, but never infer readiness from surrounding prose such
    # as "not ready", "状态表" or "Review 已返回 ready".
    if "status" not in fields:
        for line in source.splitlines():
            candidate = re.sub(r"^\s*[-*+]\s+", "", line.strip())
            match = _LOCALIZED_STATUS_RE.fullmatch(_strip_markdown_wrapper(candidate))
            if match:
                fields["status"] = match.group(1).lower()
                break
    return fields
