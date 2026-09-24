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


def sanitize_public_text(value: str, *, limit: int = 160) -> str:
    text = _ANSI_RE.sub("", str(value))
    text = _CONTROL_RE.sub(" ", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[секрет]", text)
    text = _UNIX_PATH_RE.sub("[путь]", text)
    text = _WINDOWS_PATH_RE.sub("[путь]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text
