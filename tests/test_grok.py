"""Grok weekly pool: the shape xAI's CLI proxy answered on the installation (probe of 2026-09-12),
the one-attempt-per-interval cache, and what the screen says for every way it can go wrong.

Three things are pinned here that a green screen alone would not prove: a cached number is never
older than the interval, a failed attempt is "no data" with its reason and is not retried
before the interval, and the line carries its own stamp instead of borrowing "Updated".
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from telegram_dashboard import grok
from telegram_dashboard.collect import (
    CommandResult,
    collect_all_async,
    collect_grok,
    merge_grok,
    parse_limits_payload,
)
from telegram_dashboard.compat import Environment
from telegram_dashboard.render import render_dashboard
from telegram_dashboard.schema import CapacitySummary, QuotaMetric, QuotaWindow

NOW = datetime(2026, 9, 12, 13, 40, tzinfo=UTC)
END = "2026-09-17T19:25:30.689096+00:00"

# What the proxy answered on 2026-09-12 (identity fields dropped; they are not read).
BILLING = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-10T19:25:30.689096+00:00",
            "end": END,
        },
        "creditUsagePercent": 27.0,
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "productUsage": [{"product": "GrokBuild", "usagePercent": 27.0}],
        "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
        "billingPeriodStart": "2026-09-10T19:25:30.689096+00:00",
        "billingPeriodEnd": END,
    }
}
SETTINGS = {"subscription_tier_display": "SuperGrok", "default_model": "grok-4.6"}


class FakeHttp:
    """Answers per URL; records every request so a test can count attempts."""

    def __init__(self, answers: dict[str, tuple[int, str] | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.calls.append((url, dict(headers)))
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _http(**overrides: tuple[int, str] | Exception) -> FakeHttp:
    answers: dict[str, tuple[int, str] | Exception] = {
        grok.BILLING_URL: (200, json.dumps(BILLING)),
        grok.SETTINGS_URL: (200, json.dumps(SETTINGS)),
    }
    answers.update({getattr(grok, key): value for key, value in overrides.items()})
    return FakeHttp(answers)


def _token() -> str:
    return "token-of-the-grant-inference-uses"


# ----------------------------------------------------------------------------- parsing


def test_the_probed_answer_is_one_weekly_window_with_the_provider_s_reset_date() -> None:
    window = grok.parse_weekly(BILLING, now=NOW)

    assert window == {"label": "7d", "used_percent": 27.0, "reset_at": END}
    assert grok.parse_tier(SETTINGS) == "SuperGrok"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "answer without config"),
        (
            {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY"}}},
            "period not weekly",
        ),
        (
            {"config": {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"}}},
            "no creditUsagePercent",
        ),
        (
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                    "creditUsagePercent": True,
                }
            },
            "not a number",
        ),
        (
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
                    "creditUsagePercent": 140,
                }
            },
            "outside 0..100",
        ),
        (
            {
                "config": {
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "soon"},
                    "creditUsagePercent": 5,
                }
            },
            "reset date unreadable",
        ),
    ],
)
def test_a_changed_shape_is_named_not_guessed(payload: object, reason: str) -> None:
    with pytest.raises(grok.ShapeError, match=reason):
        grok.parse_weekly(payload, now=NOW)


def test_the_reset_date_falls_back_to_the_billing_period_end() -> None:
    payload = {
        "config": {
            "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY"},
            "creditUsagePercent": 5,
            "billingPeriodEnd": END,
        }
    }

    assert grok.parse_weekly(payload, now=NOW)["reset_at"] == END


# What the proxy answered on 2026-09-25, right after the weekly reset and before the first request
# of the new period: no ``creditUsagePercent`` key at all (not null), on-demand spend zero.
AFTER_RESET_AT = datetime(2026, 9, 25, 0, 10, tzinfo=UTC)
NEXT_START = "2026-09-24T19:25:30.689096+00:00"
NEXT_END = "2026-10-01T19:25:30.689096+00:00"
AFTER_RESET_CONFIG: dict[str, Any] = {
    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": NEXT_START, "end": NEXT_END},
    "onDemandCap": {"val": 0},
    "onDemandUsed": {"val": 0},
    "isUnifiedBillingUser": True,
    "prepaidBalance": {"val": 0},
    "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
    "billingPeriodStart": NEXT_START,
    "billingPeriodEnd": NEXT_END,
}
AFTER_RESET = {"config": AFTER_RESET_CONFIG}
NOT_STARTED_WINDOW = {
    "label": "7d",
    "used_percent": None,
    "note": "usage not started",
    "reset_at": NEXT_END,
}


def _after_reset(**config: object) -> dict[str, Any]:
    return {"config": {**AFTER_RESET_CONFIG, **config}}


def test_right_after_the_weekly_reset_the_pool_says_it_has_not_started_in_words_not_a_zero() -> (
    None
):
    assert grok.parse_weekly(AFTER_RESET, now=AFTER_RESET_AT) == NOT_STARTED_WINDOW


@pytest.mark.parametrize(
    ("payload", "now", "reason"),
    [
        # The key is there but empty: a changed shape, not the counter that is not created yet.
        (_after_reset(creditUsagePercent=None), AFTER_RESET_AT, "not a number"),
        # The period is not the current one: the answer is stale or early, not a fresh week.
        (AFTER_RESET, AFTER_RESET_AT + timedelta(days=7), "no creditUsagePercent"),
        (AFTER_RESET, AFTER_RESET_AT - timedelta(days=1), "no creditUsagePercent"),
        # Something was spent: the percent must be there.
        (_after_reset(onDemandUsed={"val": 3}), AFTER_RESET_AT, "no creditUsagePercent"),
        (_after_reset(onDemandUsed={"val": False}), AFTER_RESET_AT, "no creditUsagePercent"),
        (_after_reset(onDemandUsed=None), AFTER_RESET_AT, "no creditUsagePercent"),
        # Without a readable start the period cannot be shown to be the current one.
        (
            _after_reset(
                currentPeriod={"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": NEXT_END},
                billingPeriodStart="soon",
            ),
            AFTER_RESET_AT,
            "no creditUsagePercent",
        ),
    ],
)
def test_a_missing_percent_is_a_state_only_when_every_sign_agrees(
    payload: object, now: datetime, reason: str
) -> None:
    with pytest.raises(grok.ShapeError, match=reason):
        grok.parse_weekly(payload, now=now)


# ----------------------------------------------------------------------------- one attempt


def test_one_attempt_reads_the_pool_with_the_cli_client_header_and_never_logs_the_token() -> None:
    http = _http()

    item = grok.fetch_item(now=NOW, resolve=_token, get=http)

    assert item["status"] == "available" and item["fetched_at"] == NOW.isoformat()
    assert item["windows"] == [{"label": "7d", "used_percent": 27.0, "reset_at": END}]
    assert item["plan"] == "SuperGrok"  # beside the window, never inside its label
    assert [url for url, _ in http.calls] == [grok.BILLING_URL, grok.SETTINGS_URL]
    headers = http.calls[0][1]
    assert headers["x-xai-token-auth"] == "xai-grok-cli"
    assert headers["Authorization"] == f"Bearer {_token()}"
    assert _token() not in json.dumps(item)


def test_a_week_not_started_is_an_answer_with_its_reset_date_and_the_plan_name() -> None:
    http = _http(BILLING_URL=(200, json.dumps(AFTER_RESET)))

    item = grok.fetch_item(now=AFTER_RESET_AT, resolve=_token, get=http)

    assert item["status"] == "available" and item["fetched_at"] == AFTER_RESET_AT.isoformat()
    assert item["windows"] == [NOT_STARTED_WINDOW]
    assert item["plan"] == "SuperGrok"


def test_the_tier_is_optional_and_its_absence_never_costs_the_number() -> None:
    item = grok.fetch_item(now=NOW, resolve=_token, get=_http(SETTINGS_URL=(503, "")))

    assert item["status"] == "available"
    assert item["windows"][0]["label"] == "7d"
    assert "plan" not in item


@pytest.mark.parametrize(
    ("http", "reason"),
    [
        (_http(BILLING_URL=(401, '{"error": "expired"}')), "HTTP 401"),
        (_http(BILLING_URL=(200, "<html>")), "answer shape"),
        (_http(BILLING_URL=(200, json.dumps({"config": {}}))), "period not weekly"),
        (_http(BILLING_URL=TimeoutError("read timed out")), "request failed"),
    ],
)
def test_every_failed_attempt_is_no_data_with_its_reason(http: FakeHttp, reason: str) -> None:
    item = grok.fetch_item(now=NOW, resolve=_token, get=http)

    assert item["status"] == "unavailable" and item["windows"] == []
    assert reason in item["reason"]


def test_a_token_that_cannot_be_resolved_is_named_without_the_engine_s_text_leaking_paths() -> None:
    def no_engine() -> str:
        raise ImportError("hermes_cli")

    def no_grant() -> str:
        raise LookupError("No xAI OAuth credentials stored in /var/lib/x/auth.json")

    assert (
        "xAI resolver not available"
        in grok.fetch_item(now=NOW, resolve=no_engine, get=_http())["reason"]
    )
    reason = grok.fetch_item(now=NOW, resolve=no_grant, get=_http())["reason"]
    assert reason.startswith("xAI token:") and "/var/lib" not in reason


# ----------------------------------------------------------------------------- cache policy


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
    "provider": "grok",
    "status": "available",
    "reason": None,
    "source": grok.SOURCE,
    "fetched_at": None,
    "windows": [{"label": "7d", "used_percent": 27.0, "reset_at": END}],
}
FAILED = {**AVAILABLE, "status": "unavailable", "reason": "HTTP 503", "windows": []}


def test_the_pool_is_asked_once_per_interval_and_served_from_the_cache_between() -> None:
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}

    first = grok.tick(cache, now=NOW, interval_seconds=900, fetch=fetch)
    second = grok.tick(cache, now=NOW + timedelta(minutes=5), interval_seconds=900, fetch=fetch)
    third = grok.tick(cache, now=NOW + timedelta(minutes=14), interval_seconds=900, fetch=fetch)
    fourth = grok.tick(cache, now=NOW + timedelta(minutes=15), interval_seconds=900, fetch=fetch)

    assert fetch.calls == 2
    assert first["fetched_at"] == second["fetched_at"] == third["fetched_at"] == NOW.isoformat()
    assert fourth["fetched_at"] == (NOW + timedelta(minutes=15)).isoformat()
    assert cache["attempted_at"] == fourth["fetched_at"] and cache["item"] == fourth


def test_a_failed_attempt_stays_no_data_until_the_next_interval_not_the_next_tick() -> None:
    fetch = CountingFetch(FAILED)
    cache: dict[str, Any] = {}

    items = [
        grok.tick(cache, now=NOW + timedelta(minutes=m), interval_seconds=900, fetch=fetch)
        for m in (0, 5, 10, 15)
    ]

    assert fetch.calls == 2  # minute 0 and minute 15, not every tick
    assert all(item["status"] == "unavailable" and item["reason"] == "HTTP 503" for item in items)


def test_a_cached_number_older_than_the_interval_is_not_a_number() -> None:
    """The guard behind the policy: whatever put an old number into the cache (a clock jump, a
    hand-edited state file), the screen does not show it under a fresh stamp."""
    old = {**AVAILABLE, "fetched_at": (NOW - timedelta(hours=2)).isoformat()}
    cache = {"attempted_at": NOW.isoformat(), "item": old}
    fetch = CountingFetch(AVAILABLE)

    item = grok.tick(cache, now=NOW + timedelta(minutes=1), interval_seconds=900, fetch=fetch)

    assert fetch.calls == 0
    assert item["status"] == "unavailable" and item["reason"] == "cached number too old"
    assert item["windows"] == []


def test_the_grok_line_has_its_own_source_with_its_own_freshness() -> None:
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}

    metric, source = collect_grok(cache, now=NOW, interval_seconds=900, fetch=fetch)
    failed_metric, failed_source = collect_grok(
        {}, now=NOW, interval_seconds=900, fetch=CountingFetch(FAILED)
    )

    assert metric.kind == "official" and metric.fetched_at == NOW.isoformat()
    assert (
        source.name == "grok_quota"
        and source.state == "fresh"
        and source.observed_at == NOW.isoformat()
    )
    assert failed_metric.kind == "unavailable" and failed_metric.detail == "HTTP 503"
    assert failed_source.state == "unavailable" and failed_source.detail == "HTTP 503"


NOT_STARTED = {**AVAILABLE, "windows": [NOT_STARTED_WINDOW]}


def test_a_week_not_started_is_a_fresh_source_and_its_words_survive_the_cache() -> None:
    cache: dict[str, Any] = {}

    metric, source = collect_grok(
        cache, now=NOW, interval_seconds=900, fetch=CountingFetch(NOT_STARTED)
    )
    json.loads(json.dumps(cache))  # the state file keeps the cache as JSON

    assert metric.kind == "official"
    assert metric.windows == (QuotaWindow("7d", None, NEXT_END, note="usage not started"),)
    assert source.state == "fresh"


# ----------------------------------------------------------------------------- the screen


def _render(capacity: CapacitySummary, sources=()) -> str:
    from telegram_dashboard.collect import build_snapshot
    from telegram_dashboard.schema import Coverage

    snapshot = build_snapshot(
        now=NOW,
        gateway=None,
        drift=None,
        capacity=capacity,
        sources=tuple(sources),
        incidents=(),
        coverage=Coverage(expected_profiles=1, observed_profiles=1),
    )
    return render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)


def test_each_limit_line_carries_its_own_stamp_and_its_own_reason() -> None:
    capacity = CapacitySummary(
        (
            QuotaMetric("Claude", "unavailable", detail="no account token"),
            QuotaMetric(
                "Codex",
                "official",
                windows=(QuotaWindow("Session", 14.0, "2026-09-12T16:12:00+00:00"),),
                fetched_at="2026-09-12T13:38:48+00:00",
            ),
            QuotaMetric(
                "Grok",
                "official",
                windows=(QuotaWindow("7d", 27.0, END),),
                fetched_at="2026-09-12T13:25:00+00:00",
            ),
            QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
        )
    )

    text = _render(capacity)
    lines = text.splitlines()

    assert "Claude · no data" in lines
    # The engine's ``Session`` is the five-hour window; every reset rides on its own window.
    assert "Codex 5h:14%(2h32m)" in lines
    assert "Grok 7d:27%(5d)" in lines
    assert "Gemini · no data" in lines
    # The details carry what the line does not: each number's own minute when it differs from
    # the screen's, each reason.
    assert "> Data 13:40 · Codex 13:38 · Grok 13:25" in lines
    assert "> Claude: no account token" in lines
    assert "> Gemini: source not confirmed" in lines


def test_a_week_not_started_reads_as_words_with_its_stamp_never_as_a_zero() -> None:
    window = QuotaWindow("7d", None, NEXT_END, note="usage not started")
    capacity = CapacitySummary(
        (
            QuotaMetric(
                "Grok", "official", windows=(window,), fetched_at="2026-09-12T13:25:00+00:00"
            ),
        )
    )

    lines = _render(capacity).splitlines()
    line = next(line for line in lines if line.startswith("Grok"))

    # Words, no percent, no countdown: nothing is spent yet, the reset of an unused week says
    # nothing the reader acts on.
    assert line == "Grok · usage not started"
    assert "> Data 13:40 · Grok 13:25" in lines


def test_the_facade_s_none_is_named_as_a_missing_credential_not_a_refusal() -> None:
    payload = {
        "ok": True,
        "providers": [
            {"provider": "claude", "status": "unavailable", "reason": "none", "windows": []},
            {"provider": "codex", "status": "unavailable", "reason": "AuthError", "windows": []},
        ],
    }

    capacity, _, _ = parse_limits_payload(payload, now=NOW)

    assert capacity.quotas[0].detail == "no account token"
    assert capacity.quotas[1].detail == "AuthError"
    assert [q.provider for q in capacity.quotas] == ["Claude", "Codex", "Gemini"]


def test_grok_sits_after_the_facade_providers_and_before_the_unconfirmed_ones() -> None:
    capacity = CapacitySummary(
        (
            QuotaMetric("Claude", "unavailable", detail="x"),
            QuotaMetric("Codex", "unavailable", detail="x"),
            QuotaMetric("Gemini", "unsupported", detail="source not confirmed"),
        )
    )

    merged = merge_grok(capacity, QuotaMetric("Grok", "unavailable", detail="HTTP 503"))

    assert [q.provider for q in merged.quotas] == ["Claude", "Codex", "Grok", "Gemini"]


# ----------------------------------------------------------------------------- the tick


def _env(tmp_path: Path, *, limits_enabled: bool) -> Environment:
    payload = {
        "pid": 4242,
        "gateway_state": "running",
        "updated_at": "2026-09-12T13:38:00+00:00",
        "platforms": {"telegram": {"state": "running", "writer_pid": 4242}},
    }
    (tmp_path / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return Environment(hermes_home=tmp_path, limits_enabled=limits_enabled)


class Runner:
    def run(self, argv, *, timeout_seconds):
        return CommandResult(0, "", "")


def _facade():
    """What the engine facade answers on the installation: no Anthropic credential (``None``),
    a Codex snapshot; the resolver the tick would import is replaced by this one."""
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


def test_the_tick_keeps_the_grok_cache_in_the_caller_s_dict_and_counts_the_source(
    tmp_path: Path,
) -> None:
    fetch = CountingFetch(AVAILABLE)
    cache: dict[str, Any] = {}
    env = _env(tmp_path, limits_enabled=True)

    async def two_ticks():
        first = await collect_all_async(
            env,
            Runner(),
            now=NOW,
            resolve_limits=_facade,
            grok_cache=cache,
            grok_interval_seconds=900,
            grok_fetch=fetch,
        )
        second = await collect_all_async(
            env,
            Runner(),
            now=NOW + timedelta(minutes=5),
            resolve_limits=_facade,
            grok_cache=cache,
            grok_interval_seconds=900,
            grok_fetch=fetch,
        )
        return first, second

    first, second = asyncio.run(two_ticks())

    assert fetch.calls == 1 and cache["attempted_at"] == NOW.isoformat()
    names = [source.name for source in first.sources]
    assert names == ["gateway_state", "limits", "grok_quota", "kimi_quota", "drift", "backup"]
    grok_quota = next(q for q in second.capacity.quotas if q.provider == "Grok")
    assert grok_quota.kind == "official" and grok_quota.fetched_at == NOW.isoformat()
    text = render_dashboard(second, now=NOW, zone=UTC, period_seconds=300)
    lines = text.splitlines()
    assert "Grok 7d:27%(5d)" in lines
    # Both cached numbers keep their own minute next to the screen's.
    assert "> Data 13:45 · Codex 13:40 · Grok 13:40" in lines
    assert "Claude · no data" in lines and "> Claude: no account token" in lines
    seen = [s for s in second.sources if s.state in ("fresh", "stale")]
    assert "grok_quota" in [s.name for s in seen]
    assert f"sources {len(seen)}/6" in text  # six sources, Grok counted


def test_a_week_not_started_counts_the_source_and_leaves_no_gap_on_the_screen(
    tmp_path: Path,
) -> None:
    snapshot = asyncio.run(
        collect_all_async(
            _env(tmp_path, limits_enabled=True),
            Runner(),
            now=NOW,
            resolve_limits=_facade,
            grok_cache={},
            grok_fetch=CountingFetch(NOT_STARTED),
        )
    )

    text = render_dashboard(snapshot, now=NOW, zone=UTC, period_seconds=300)
    assert "Grok · usage not started" in text.splitlines()
    assert next(s for s in snapshot.sources if s.name == "grok_quota").state == "fresh"
    assert "creditUsagePercent" not in text


def test_with_limits_off_grok_is_off_too_and_says_why(tmp_path: Path) -> None:
    fetch = CountingFetch(AVAILABLE)

    snapshot = asyncio.run(
        collect_all_async(_env(tmp_path, limits_enabled=False), Runner(), now=NOW, grok_fetch=fetch)
    )

    assert fetch.calls == 0
    grok_quota = next(q for q in snapshot.capacity.quotas if q.provider == "Grok")
    assert grok_quota.kind == "unsupported" and grok_quota.detail == "limits disabled in config"
    assert next(s for s in snapshot.sources if s.name == "grok_quota").state == "unsupported"


def test_a_grok_worker_past_its_deadline_is_one_tick_of_no_data_with_the_reason(
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
            grok_cache=cache,
            grok_fetch=slow,
            grok_timeout_seconds=0.05,
        )
    )

    grok_quota = next(q for q in snapshot.capacity.quotas if q.provider == "Grok")
    assert grok_quota.kind == "unavailable" and "no answer within" in (grok_quota.detail or "")
    # The abandoned worker still finished (the loop waits for its executor on shutdown) and left
    # its attempt in the cache: the next tick serves it instead of asking the proxy again.
    assert cache["item"]["status"] == "available"
