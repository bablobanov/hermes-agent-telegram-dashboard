"""Kimi Code quota: the shape the official client parses (``@moonshot-ai/kimi-code-oauth``,
``managed-usage.ts``: ``usages.{limit_5h,limit_7d,limit_month_total,limit_month_code}`` with
``used_ratio`` 0..1 and ``reset_time``), read from the same host and with the same key the engine
uses for inference, on the one-attempt-per-interval cache shared with Grok.

Pinned here, beyond a green screen: the URL is derived from the engine's own base URL (no second
host in code), the key never reaches the item or a log, every window keeps its own reset date on
the line, and the Kimi cache never shares a dict with the Grok cache.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from telegram_dashboard import kimi
from telegram_dashboard.collect import (
    CommandResult,
    collect_all_async,
    collect_kimi,
    merge_quotas,
)
from telegram_dashboard.compat import Environment
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import CapacitySummary, QuotaMetric, QuotaWindow

NOW = datetime(2026, 9, 22, 13, 40, tzinfo=UTC)
RESET_5H = "2026-09-22T16:32:14Z"
RESET_7D = "2026-09-26T12:32:14Z"
RESET_MONTH = "2026-10-22T00:00:00Z"

# A legacy plan as reported by a third-party client (codenotch#307): 5h and weekly windows plus
# the older ``usage``/``limits`` blocks the official parser ignores.
LEGACY = {
    "usage": {"limit": "100", "remaining": "60", "resetTime": "2026-09-26T12:32:15.376980Z"},
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "remaining": "88", "resetTime": "2026-09-22T16:32:15Z"},
        }
    ],
    "usages": {
        "limit_5h": {"used_ratio": 0.12, "reset_time": RESET_5H},
        "limit_7d": {"used_ratio": 0.4, "reset_time": RESET_7D},
    },
}
# A new plan as reported by another client (oh-my-pi#12790): weekly window gone, monthly total
# with its code share.
NEW_PLAN = {
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": "100", "used": "12", "resetTime": "2026-09-22T16:32:15Z"},
        }
    ],
    "usages": {
        "limit_5h": {"used_ratio": 0.12, "reset_time": RESET_5H},
        "limit_month_total": {"used_ratio": 0.0795, "reset_time": RESET_MONTH},
        "limit_month_code": {"used_ratio": 0.03, "reset_time": RESET_MONTH},
    },
}
BASE_URL = "https://api.kimi.example/coding"
USAGES_URL = "https://api.kimi.example/coding/v1/usages"


class FakeHttp:
    def __init__(self, answers: dict[str, tuple[int, str] | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append((url, dict(headers)))
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _http(answer: tuple[int, str] | Exception | None = None) -> FakeHttp:
    return FakeHttp({USAGES_URL: (200, json.dumps(LEGACY)) if answer is None else answer})


def _creds() -> tuple[str, str]:
    return "sk-kimi-the-key-inference-uses", BASE_URL


# ----------------------------------------------------------------------------- the URL


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.kimi.example/coding",
        "https://api.kimi.example/coding/",
        "https://api.kimi.example/coding/v1",
        "https://api.kimi.example/coding/v1/",
    ],
)
def test_the_usages_url_is_derived_from_the_engine_s_base_url_not_a_second_host(
    base_url: str,
) -> None:
    assert kimi.usages_url(base_url) == USAGES_URL


# ----------------------------------------------------------------------------- parsing


def test_a_legacy_plan_is_a_5h_window_and_a_weekly_window_with_their_own_resets() -> None:
    assert kimi.parse_usages(LEGACY) == [
        {"label": "5h", "used_percent": 12.0, "reset_at": RESET_5H},
        {"label": "week", "used_percent": 40.0, "reset_at": RESET_7D},
    ]


def test_a_new_plan_is_a_5h_window_and_a_monthly_window_the_code_share_is_not_a_window() -> None:
    windows = kimi.parse_usages(NEW_PLAN)

    assert [w["label"] for w in windows] == ["5h", "month"]
    assert windows[1]["used_percent"] == pytest.approx(7.95)
    assert windows[1]["reset_at"] == RESET_MONTH


def test_a_window_without_a_reset_keeps_its_number() -> None:
    windows = kimi.parse_usages({"usages": {"limit_5h": {"used_ratio": 0.5}}})

    assert windows == [{"label": "5h", "used_percent": 50.0, "reset_at": None}]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "answer without usages"),
        ({"usages": []}, "answer without usages"),
        ({"usages": {}}, "usages without known windows"),
        ({"usages": {"limit_9y": {"used_ratio": 0.1}}}, "usages without known windows"),
        ({"usages": {"limit_5h": "12%"}}, "limit_5h not an object"),
        ({"usages": {"limit_5h": {"reset_time": RESET_5H}}}, "limit_5h.used_ratio not a number"),
        ({"usages": {"limit_5h": {"used_ratio": True}}}, "limit_5h.used_ratio not a number"),
        ({"usages": {"limit_5h": {"used_ratio": 12}}}, "limit_5h.used_ratio outside 0..1"),
        (
            {"usages": {"limit_5h": {"used_ratio": 0.1, "reset_time": "soon"}}},
            "limit_5h.reset_time unreadable",
        ),
    ],
)
def test_a_changed_shape_is_named_not_guessed(payload: object, reason: str) -> None:
    with pytest.raises(kimi.ShapeError, match=reason):
        kimi.parse_usages(payload)


# ----------------------------------------------------------------------------- one attempt


def test_one_attempt_sends_the_engine_s_client_header_with_the_bearer_key_and_never_logs_it() -> (
    None
):
    http = _http()

    item = kimi.fetch_item(now=NOW, resolve=_creds, get=http)

    assert item["status"] == "available" and item["fetched_at"] == NOW.isoformat()
    assert item["source"] == kimi.SOURCE and item["provider"] == "kimi"
    assert [w["label"] for w in item["windows"]] == ["5h", "week"]
    assert [url for url, _ in http.calls] == [USAGES_URL]
    headers = http.calls[0][1]
    assert headers["Authorization"] == f"Bearer {_creds()[0]}"
    assert headers["User-Agent"] == kimi.CLIENT_HEADER[1]
    assert _creds()[0] not in json.dumps(item)


@pytest.mark.parametrize(
    ("http", "reason"),
    [
        (_http((401, '{"error": "unauthorized"}')), "HTTP 401"),
        (_http((403, "")), "HTTP 403"),
        (_http((200, "<html>")), "answer shape"),
        (_http((200, json.dumps({"usages": {}}))), "usages without known windows"),
        (_http(TimeoutError("read timed out")), "request failed"),
    ],
)
def test_every_failed_attempt_is_no_data_with_its_reason(http: FakeHttp, reason: str) -> None:
    item = kimi.fetch_item(now=NOW, resolve=_creds, get=http)

    assert item["status"] == "unavailable" and item["windows"] == []
    assert reason in item["reason"]


def test_a_key_that_cannot_be_resolved_is_named_without_the_engine_s_text_leaking_paths() -> None:
    def no_engine() -> tuple[str, str]:
        raise ImportError("hermes_cli")

    def no_key() -> tuple[str, str]:
        raise LookupError("no credential in /var/lib/x/auth.json")

    http = _http()
    assert (
        "Kimi resolver not available"
        in kimi.fetch_item(now=NOW, resolve=no_engine, get=http)["reason"]
    )
    reason = kimi.fetch_item(now=NOW, resolve=no_key, get=http)["reason"]
    assert reason.startswith("Kimi key:") and "/var/lib" not in reason
    assert http.calls == []


# ----------------------------------------------------------------------------- the source


class CountingFetch:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls = 0

    def __call__(self, *, now: datetime) -> dict[str, Any]:
        self.calls += 1
        return {
            **self.item,
            "fetched_at": now.isoformat() if self.item["status"] == "available" else None,
        }


AVAILABLE = {
    "provider": "kimi",
    "status": "available",
    "reason": None,
    "source": kimi.SOURCE,
    "fetched_at": None,
    "windows": [
        {"label": "5h", "used_percent": 12.0, "reset_at": RESET_5H},
        {"label": "week", "used_percent": 40.0, "reset_at": RESET_7D},
    ],
}
FAILED = {**AVAILABLE, "status": "unavailable", "reason": "HTTP 503", "windows": []}


def test_the_kimi_line_has_its_own_source_with_its_own_freshness_and_its_own_cache() -> None:
    cache: dict[str, Any] = {}
    fetch = CountingFetch(AVAILABLE)

    metric, source = collect_kimi(cache, now=NOW, interval_seconds=900, fetch=fetch)
    again, _ = collect_kimi(
        cache, now=NOW + timedelta(minutes=5), interval_seconds=900, fetch=fetch
    )
    failed_metric, failed_source = collect_kimi(
        {}, now=NOW, interval_seconds=900, fetch=CountingFetch(FAILED)
    )

    assert fetch.calls == 1 and again.fetched_at == NOW.isoformat()
    assert metric.provider == "Kimi" and metric.kind == "official"
    assert source.name == "kimi_quota" and source.state == "fresh"
    assert cache["item"]["provider"] == "kimi"
    assert failed_metric.kind == "unavailable" and failed_metric.detail == "HTTP 503"
    assert failed_source.state == "unavailable"


def test_kimi_sits_after_grok_and_before_the_unconfirmed_providers() -> None:
    capacity = CapacitySummary(
        (
            QuotaMetric("Claude", "unavailable", detail="x"),
            QuotaMetric("Codex", "unavailable", detail="x"),
            QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
        )
    )

    merged = merge_quotas(
        capacity,
        QuotaMetric("Grok", "unavailable", detail="HTTP 503"),
        QuotaMetric("Kimi", "unavailable", detail="HTTP 503"),
    )

    assert [q.provider for q in merged.quotas] == ["Claude", "Codex", "Grok", "Kimi", "Gemini"]


# ----------------------------------------------------------------------------- the screen


def _render(capacity: CapacitySummary) -> str:
    from telegram_dashboard.collect import build_snapshot
    from telegram_dashboard.schema import Coverage

    snapshot = build_snapshot(
        now=NOW,
        gateway=None,
        drift=None,
        capacity=capacity,
        sources=(),
        incidents=(),
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
    )
    return render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)


def test_a_line_with_several_windows_shows_each_window_s_own_reset() -> None:
    capacity = CapacitySummary(
        (
            QuotaMetric(
                "Kimi",
                "official",
                windows=(QuotaWindow("5h", 12.0, RESET_5H), QuotaWindow("week", 40.0, RESET_7D)),
                fetched_at="2026-09-22T13:25:00+00:00",
            ),
            QuotaMetric(
                "Grok",
                "official",
                windows=(QuotaWindow("week", 27.0, "2026-09-24T19:25:30+00:00"),),
                fetched_at="2026-09-22T13:25:00+00:00",
            ),
        )
    )

    text = _render(capacity)

    # The most spent window gets the bar and its label; the other follows in words.
    assert "Kimi ▓▓░░░ 40% week · 5h 12%" in text.splitlines()
    assert "Grok ▓░░░░ 27%" in text.splitlines()
    # Resets and stamps live in the details: a reset today is a time, a later one a date.
    assert "> Kimi 5h 16:32 · week 26.09" in text.splitlines()
    assert "> Grok 24.09" in text.splitlines()
    assert "> Данные 13:40 · Kimi 13:25 · Grok 13:25" in text.splitlines()


def test_a_window_without_a_reset_says_so_beside_the_others() -> None:
    capacity = CapacitySummary(
        (
            QuotaMetric(
                "Kimi",
                "official",
                windows=(QuotaWindow("5h", 12.0, None), QuotaWindow("month", 8.0, RESET_MONTH)),
            ),
        )
    )

    text = _render(capacity)

    assert "Kimi ▓░░░░ 12% 5h · month 8%" in text.splitlines()
    assert "> Kimi month 22.10" in text.splitlines()  # only the window that has a reset


# ----------------------------------------------------------------------------- the tick


def _env(tmp_path: Path, *, limits_enabled: bool) -> Environment:
    payload = {
        "pid": 4242,
        "gateway_state": "running",
        "updated_at": "2026-09-22T13:38:00+00:00",
        "platforms": {"telegram": {"state": "running", "writer_pid": 4242}},
    }
    (tmp_path / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return Environment(hermes_home=tmp_path, limits_enabled=limits_enabled)


class Runner:
    def run(self, argv, *, timeout_seconds):
        return CommandResult(0, "", "")


def _facade():
    from types import SimpleNamespace

    def fetch(provider: str):
        if provider != "openai-codex":
            return None
        window = SimpleNamespace(label="Session", used_percent=14.0, reset_at=None)
        return SimpleNamespace(
            available=True,
            unavailable_reason=None,
            source="codex",
            fetched_at=NOW,
            windows=[window],
        )

    return fetch


def test_the_tick_reads_kimi_on_its_own_cache_and_the_screen_carries_the_line(
    tmp_path: Path,
) -> None:
    fetch = CountingFetch(AVAILABLE)
    grok_cache: dict[str, Any] = {}
    kimi_cache: dict[str, Any] = {}

    snapshot = asyncio.run(
        collect_all_async(
            _env(tmp_path, limits_enabled=True),
            Runner(),
            now=NOW,
            resolve_limits=_facade,
            grok_cache=grok_cache,
            kimi_cache=kimi_cache,
            kimi_interval_seconds=900,
            kimi_fetch=fetch,
        )
    )

    assert fetch.calls == 1 and kimi_cache["attempted_at"] == NOW.isoformat()
    assert grok_cache.get("item", {}).get("provider") != "kimi"
    names = [source.name for source in snapshot.sources]
    assert names == ["gateway_state", "limits", "grok_quota", "kimi_quota", "drift", "backup"]
    quota = next(q for q in snapshot.capacity.quotas if q.provider == "Kimi")
    assert quota.kind == "official" and quota.fetched_at == NOW.isoformat()
    text = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)
    assert "Kimi ▓▓░░░ 40% week · 5h 12%" in text.splitlines()
    assert "> Kimi 5h 16:32 · week 26.09" in text.splitlines()
    assert "Данные 13:40" not in text  # read at the screen's own minute: nothing to add
    seen = [s for s in snapshot.sources if s.state in ("fresh", "stale")]
    assert "kimi_quota" in [s.name for s in seen]
    assert f"источники {len(seen)}/6" in text


def test_with_limits_off_kimi_is_off_too_and_says_why(tmp_path: Path) -> None:
    fetch = CountingFetch(AVAILABLE)

    snapshot = asyncio.run(
        collect_all_async(_env(tmp_path, limits_enabled=False), Runner(), now=NOW, kimi_fetch=fetch)
    )

    assert fetch.calls == 0
    quota = next(q for q in snapshot.capacity.quotas if q.provider == "Kimi")
    assert quota.kind == "unsupported" and quota.detail == "limits disabled in config"
    assert next(s for s in snapshot.sources if s.name == "kimi_quota").state == "unsupported"


def test_a_kimi_worker_past_its_deadline_is_one_tick_of_no_data_with_the_reason(
    tmp_path: Path,
) -> None:
    import time

    def slow(*, now: datetime) -> dict[str, Any]:
        time.sleep(0.3)
        return {**AVAILABLE, "fetched_at": now.isoformat()}

    cache: dict[str, Any] = {}
    snapshot = asyncio.run(
        collect_all_async(
            _env(tmp_path, limits_enabled=True),
            Runner(),
            now=NOW,
            resolve_limits=_facade,
            kimi_cache=cache,
            kimi_fetch=slow,
            kimi_timeout_seconds=0.05,
        )
    )

    quota = next(q for q in snapshot.capacity.quotas if q.provider == "Kimi")
    assert quota.kind == "unavailable" and "no answer within" in (quota.detail or "")
    assert cache["item"]["status"] == "available"
