import json
import re


DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "direct_answer",
        "description": (
            "Use this for conversation, reactions, opinions, jokes, general knowledge, "
            "and statements that do not request a device action or current-state lookup. "
            "Put the complete user-facing reply in the response argument."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "response": {
                    "type": "string",
                    "description": "The complete user-facing response.",
                },
            },
            "required": ["response"],
        },
    },
}


_RESPONSE_PREFIX = re.compile(r'"response"\s*:\s*"')
_GARBAGE_CHARS = frozenset('")\'}）')


def extract_response(arguments):
    """Return a complete or partially streamed direct-answer response."""
    if not arguments or not isinstance(arguments, str):
        return ""

    try:
        data = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        data = None

    if isinstance(data, dict):
        response = data.get("response")
        return response if isinstance(response, str) else ""

    match = _RESPONSE_PREFIX.search(arguments)
    if match is None:
        return ""

    raw = arguments[match.end():]
    if raw.endswith('"}'):
        raw = raw[:-2]
    elif raw.endswith('"'):
        raw = raw[:-1]

    return raw.replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')


def clean_response_text(text):
    """Remove short JSON closure fragments accidentally included in speech."""
    if not isinstance(text, str) or not text:
        return ""

    cleaned_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and len(stripped) <= 8 and all(
            char in _GARBAGE_CHARS for char in stripped
        ):
            continue
        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)
    return re.sub(r'["\'}\]]+$', "", result.rstrip()).rstrip()
