from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|ghp|github_pat|xox[baprs]|AIza)[-_][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*", re.IGNORECASE),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
)
_UNIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:home|Users|var|root|etc|opt|srv|tmp|mnt|data)/[^\s]+"
)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Delivery targets and chat ids (decision of 01.10): ``<platform>:<chat>[:<topic>]`` as the engine
# writes cron ``deliver`` and delivery errors (any word before the colon counts, so a new platform
# needs no release), and a bare run of nine or more digits, with or without a minus (a Telegram
# chat or user id), when it is not part of a word, a hash, a decimal or a date. Applied after the
# secrets (a bot token is digits and a colon too) and the paths.
_TARGET_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,23}:-?\d{5,}(?::\d+)*\b")
_ID_RE = re.compile(r"(?<![\w.])-?\d{9,}(?![\w.])")


def sanitize_public_text(value: str, *, limit: int = 160) -> str:
    text = _ANSI_RE.sub("", str(value))
    text = _CONTROL_RE.sub(" ", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[secret]", text)
    text = _UNIX_PATH_RE.sub("[path]", text)
    text = _WINDOWS_PATH_RE.sub("[path]", text)
    text = _TARGET_RE.sub("[target]", text)
    text = _ID_RE.sub("[id]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text
