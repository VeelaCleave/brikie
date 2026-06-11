from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


def repair_json(raw: str) -> Optional[str]:
    """Attempt to repair common JSON syntax errors from LLM output.

    Fixes applied (in order):
    1. Strip leading/trailing whitespace and code fences
    2. Replace single quotes with double quotes
    3. Wrap unquoted keys in double quotes
    4. Strip trailing commas in objects/arrays
    5. Add missing closing braces/backets
    """
    if not raw or not isinstance(raw, str):
        return None

    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Already valid?
    if _is_valid_json(text):
        return text

    # Replace single quotes with double quotes (but not inside strings)
    text = _fix_single_quotes(text)

    # Wrap bare keys in double quotes
    text = _wrap_keys(text)

    # Strip trailing commas
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r",\s*]", "]", text)

    # Add missing closing brace/backet
    opens = text.count("{") + text.count("[")
    closes = text.count("}") + text.count("]")
    if opens > closes:
        text += "}" * (opens - closes)

    if _is_valid_json(text):
        return text

    # Final attempt: try to extract JSON object from any context
    extracted = _extract_json_object(text)
    if extracted:
        return extracted

    return None


def validate_args_against_schema(
    args: Dict[str, Any],
    schema: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate and coerce arguments against a JSON schema.

    Returns:
        Tuple of (fixed_args, warnings).
    """
    if not isinstance(args, dict):
        return {}, ["args is not a dict"]

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    fixed: Dict[str, Any] = {}
    warnings: List[str] = []

    for key, value in args.items():
        if key not in props:
            warnings.append(f"Dropped unknown key '{key}'")
            continue

        prop = props[key]
        expected_type = prop.get("type", "string")

        coerced = _coerce_type(value, expected_type)
        if coerced != value:
            warnings.append(f"Coerced '{key}' from {type(value).__name__} to {expected_type}")

        fixed[key] = coerced

    # Fill missing params that have defaults
    for key, prop in props.items():
        if key not in fixed and "default" in prop:
            fixed[key] = prop["default"]
            warnings.append(f"Filled missing '{key}' with default")

    return fixed, warnings


def fuzzy_name_match(
    name: str,
    registered_names: List[str],
    max_distance: int = 2,
) -> Optional[str]:
    """Find the closest registered tool name by Levenshtein distance.

    Args:
        name: The (possibly mispelled) tool name.
        registered_names: List of valid tool names.
        max_distance: Maximum edit distance to consider a match (default 2).

    Returns:
        The best matching name, or None if no match within max_distance.
    """
    if not name or not registered_names:
        return None

    best_match: Optional[str] = None
    best_dist = max_distance + 1

    for candidate in registered_names:
        dist = _levenshtein(name.lower(), candidate.lower())
        if dist < best_dist:
            best_dist = dist
            best_match = candidate
            if best_dist == 0:
                break

    return best_match


def should_retry(error_message: str) -> bool:
    """Determine if a runtime error is worth retrying.

    Retryable:
    - Transient network errors ("timeout", "connection reset", "temporary")
    - Rate limiting ("rate limit", "too many requests")
    - Temporary file/IO errors ("resource temporarily unavailable")

    Not retryable:
    - Auth/permission errors
    - Semantic/logical errors
    - "No ToolBrick found"
    """
    if not error_message:
        return False

    lowered = error_message.lower()

    retryable_keywords = [
        "timed out",
        "timeout",
        "connection reset",
        "temporary",
        "rate limit",
        "too many requests",
        "resource temporarily",
        "try again later",
    ]

    non_retryable_keywords = [
        "no toolbrick found",
        "permission denied",
        "unauthorized",
        "invalid api key",
        "not found",
        "does not exist",
    ]

    for keyword in non_retryable_keywords:
        if keyword in lowered:
            return False

    for keyword in retryable_keywords:
        if keyword in lowered:
            return True

    return False


# ── Internal helpers ───────────────────────────────────────────────────


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _fix_single_quotes(text: str) -> str:
    """Replace single quotes with double quotes, preserving escaped quotes."""
    # Strategy: alternate between in-string and out-of-string
    # Simpler: just replace ' with " for dict keys and string values
    result = []
    in_string = False
    escape = False

    for ch in text:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == "\\":
            result.append(ch)
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            result.append(ch)
        elif ch == "'" and not in_string:
            result.append('"')
        else:
            result.append(ch)

    return "".join(result)


_KEY_PATTERN = re.compile(r"(?<!\w)([a-zA-Z_][a-zA-Z0-9_]*)\s*:")


def _wrap_keys(text: str) -> str:
    """Wrap bare JavaScript-style keys in double quotes."""
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        return f'"{key}":'

    return _KEY_PATTERN.sub(_replace, text)


_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> Optional[str]:
    """Try to extract a JSON object from surrounding text."""
    match = _JSON_OBJECT_PATTERN.search(text)
    if match:
        candidate = match.group(0)
        if _is_valid_json(candidate):
            return candidate
    return None


def _coerce_type(value: Any, expected_type: str) -> Any:
    """Coerce a value to the expected JSON Schema type."""
    if expected_type == "string":
        if not isinstance(value, str):
            return str(value)
        return value

    if expected_type in ("number", "integer"):
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return int(value) if expected_type == "integer" else float(value)
            except (ValueError, TypeError):
                pass
        return value

    if expected_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        if isinstance(value, (int, float)):
            return value != 0
        return value

    if expected_type == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    if expected_type == "object":
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return value

    return value


def _levenshtein(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if len(s2) == 0:
        return len(s1)

    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr.append(min(
                curr[j] + 1,
                prev[j + 1] + 1,
                prev[j] + cost,
            ))
        prev = curr

    return prev[-1]
