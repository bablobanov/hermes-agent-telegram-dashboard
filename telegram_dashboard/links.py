from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ALLOWED_PATHS = frozenset({"/", "/sessions", "/analytics", "/logs", "/cron", "/skills", "/system"})


def build_dashboard_link(
    base_url: str | None,
    *,
    path: str = "/",
    profile: str | None = None,
) -> str | None:
    if not base_url or path not in _ALLOWED_PATHS:
        return None
    if any(ord(char) < 32 for char in base_url):
        return None
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if profile is not None and _PROFILE_RE.fullmatch(profile) is None:
        return None
    prefix = parsed.path.rstrip("/")
    full_path = f"{prefix}{path}" if path != "/" else (prefix or "/")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if profile is not None:
        query["profile"] = profile
    return urlunsplit((parsed.scheme, parsed.netloc, full_path, urlencode(query), ""))
