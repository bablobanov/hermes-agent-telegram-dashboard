"""Read-only collectors on stable surfaces.

Gateway state and drift are files and a script. Limits are the one named exception: the engine's
public usage facade, imported at call time and called the way ``/usage`` calls it (``limits.py``).

Each collector returns three things: the block for the snapshot, a ``SourceObservation`` that
says how much to trust it, and the incidents it noticed. A source that cannot prove a value says
``unknown``; a source that does not exist on this installation says ``unsupported``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .backup import BackupPart, collect_backup
from .compat import Environment, ProbeResult
from .grok import DEFAULT_INTERVAL_SECONDS as GROK_DEFAULT_INTERVAL_SECONDS
from .grok import TICK_TIMEOUT_SECONDS as GROK_TICK_TIMEOUT_SECONDS
from .grok import fetch_item as grok_fetch_item
from .kimi import DEFAULT_INTERVAL_SECONDS as KIMI_DEFAULT_INTERVAL_SECONDS
from .kimi import TICK_TIMEOUT_SECONDS as KIMI_TICK_TIMEOUT_SECONDS
from .kimi import fetch_item as kimi_fetch_item
from .limits import Resolver, build_limits_payload, fetch_limits_payload_off_loop, import_fetcher
from .normalize import classify_freshness, derive_overall
from .policy import sanitize_public_text
from .quota_cache import Fetch as QuotaFetch
from .quota_cache import tick as quota_tick
from .schema import (
    Authority,
    BackupSummary,
    CapacitySummary,
    Coverage,
    DashboardSnapshot,
    DriftSummary,
    GatewaySummary,
    Incident,
    PlatformState,
    QuotaMetric,
    QuotaWindow,
    SourceObservation,
    SourceState,
)
from .workers import Flights, StillRunning, failure_name

logger = logging.getLogger(__name__)

# check_drift runs daily; anything older than a day plus slack is stale.
DRIFT_STALE_SECONDS = 26 * 3600
LIMITS_STALE_SECONDS = 15 * 60

_RUNNING_PLATFORM = frozenset({"running", "connected", "ok", "ready"})
_FATAL_PLATFORM = frozenset({"fatal", "degraded", "error", "failed"})
_DOWN_PLATFORM = frozenset({"disconnected", "connecting", "retrying", "paused", "disabled"})
_STOPPED_GATEWAY = frozenset({"stopped", "exited", "stopping", "crashed", "failed"})

_SEVERITY_RANK = {"critical": 0, "warning": 1, "unknown": 2, "normal": 3}
# Providers the engine's usage facade answers for; Grok and Kimi are read by ``grok.py`` and
# ``kimi.py`` on their own cadence, each as its own source.
_PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}
GROK_LABEL = "Grok"
KIMI_LABEL = "Kimi"
GrokFetch = QuotaFetch
KimiFetch = QuotaFetch
# Nobody has looked for a quota surface of these yet; the screen says so, not "0%".
_UNCONFIRMED_PROVIDERS = ("Gemini",)
# What the facade's ``None`` means (``_fetch_anthropic_account_usage`` returns it only when no
# token resolves): the installation has no credential, not a provider that refused.
_NO_CREDENTIAL = "у сервера нет учётного токена"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None


class CommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult: ...


class SubprocessRunner:
    """No shell, own process group, hard timeout with process-tree kill, never raises."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        popen_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            popen_kwargs["creationflags"] = flags | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **popen_kwargs,
            )
        except (OSError, ValueError) as exc:
            return CommandResult(None, "", "", error=type(exc).__name__)
        try:
            out, err = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            return CommandResult(None, out or "", err or "", timed_out=True, error="timeout")
        return CommandResult(proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


# ----------------------------------------------------------------------------- gateway state


PidProbe = Callable[[int], bool | None]


def default_pid_probe(pid: int) -> bool | None:
    """True/False when liveness can be asked (POSIX signal 0); None where it cannot (Windows)."""
    if sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def collect_gateway(
    env: Environment, *, now: datetime, pid_probe: PidProbe = default_pid_probe
) -> tuple[GatewaySummary, SourceObservation, tuple[Incident, ...]]:
    """``gateway_state.json`` is written on state transitions only (no periodic heartbeat in
    0.21.1), so ``updated_at`` age is NOT a death signal. Liveness comes from the recorded pid."""
    path = env.hermes_home / "gateway_state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        summary = GatewaySummary(
            "unsupported", "unsupported", detail="gateway_state.json отсутствует"
        )
        source = SourceObservation(
            "gateway_state", "official", "unsupported", detail=summary.detail
        )
        return summary, source, ()
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _gateway_unreadable(type(exc).__name__)
    if not isinstance(payload, dict):
        return _gateway_unreadable("not an object")

    updated_at = payload.get("updated_at")
    updated_text = updated_at if isinstance(updated_at, str) else None
    pid = payload.get("pid")
    alive = pid_probe(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
    process = _gateway_process(payload.get("gateway_state"), alive=alive)
    telegram, error_code, needs_attention = _telegram_platform(payload.get("platforms"), pid)
    if alive is None:
        # Without liveness the platform entry is a claim by a process we cannot see.
        telegram, needs_attention = "unknown", False

    incidents: list[Incident] = []
    if alive is False:
        incidents.append(
            Incident(
                "gateway:dead", "critical", "Процесс gateway не найден, статус пережил владельца"
            )
        )
    if process == "running" and telegram in ("degraded", "disconnected"):
        reason = error_code or telegram
        incidents.append(
            Incident(
                "telegram:polling",
                "critical",
                f"Gateway работает, но Telegram не подключён ({reason})",
            )
        )
    elif needs_attention:
        incidents.append(
            Incident("telegram:attention", "warning", "Telegram-адаптер просит внимания")
        )
    state: SourceState
    if alive is True:
        state = "fresh"
    elif alive is False:
        state = "stale"
    else:
        state = "unavailable"
    detail = error_code if alive is not None else "живость pid не проверить на этой платформе"
    summary = GatewaySummary(process, telegram, updated_text, detail=detail)
    source = SourceObservation(
        "gateway_state", "official", state, observed_at=updated_text, detail=detail
    )
    return summary, source, tuple(incidents)


def _gateway_unreadable(
    reason: str,
) -> tuple[GatewaySummary, SourceObservation, tuple[Incident, ...]]:
    summary = GatewaySummary("unknown", "unknown", detail=f"нечитаем: {reason}")
    source = SourceObservation("gateway_state", "official", "unavailable", detail=summary.detail)
    incident = Incident("gateway:unreadable", "warning", "gateway_state.json не читается")
    return summary, source, (incident,)


def _gateway_process(raw: object, *, alive: bool | None) -> Any:
    if alive is False:
        return "stopped"
    if alive is None:
        return "unknown"
    state = str(raw or "").lower()
    if state == "running":
        return "running"
    if state in _STOPPED_GATEWAY:
        return "stopped"
    return "unknown"


def _telegram_platform(
    platforms: object, writer_pid: object
) -> tuple[PlatformState, str | None, bool]:
    if not isinstance(platforms, dict):
        return "unknown", None, False
    entry: dict[str, Any] | None = None
    for key, value in platforms.items():
        is_telegram = isinstance(key, str) and (key == "telegram" or key.endswith(":telegram"))
        if is_telegram and isinstance(value, dict):
            entry = value
            break
    if entry is None:
        return "unknown", None, False
    entry_writer = entry.get("writer_pid")
    if entry_writer is not None and writer_pid is not None and entry_writer != writer_pid:
        # Preserved from an earlier process; it does not describe the current gateway.
        return "unknown", None, False
    raw = str(entry.get("state") or "").lower()
    state: PlatformState
    if raw in _RUNNING_PLATFORM:
        state = "connected"
    elif raw in _FATAL_PLATFORM:
        state = "degraded"
    elif raw in _DOWN_PLATFORM:
        state = "disconnected"
    else:
        state = "unknown"
    code = entry.get("error_code")
    code_text = sanitize_public_text(str(code), limit=40) if code else None
    return state, code_text, bool(entry.get("needs_attention"))


# ----------------------------------------------------------------------------- drift

_SECTION_RE = re.compile(r"^\[(\d)\][^:\n]*:\s*(\d+)\s*$", re.MULTILINE)
_KEYS_RE = re.compile(r"ключей\s+(\d+)")


def parse_drift_output(
    stdout: str, exit_code: int | None, *, checked_at: str | None
) -> DriftSummary:
    """Reduce check_drift.py output to a number. Exit 2 or unparseable output is ``unknown``."""
    if exit_code not in (0, 1):
        return DriftSummary("unknown", checked_at=checked_at, detail=f"код возврата {exit_code}")
    sections = {int(number): int(count) for number, count in _SECTION_RE.findall(stdout)}
    keys = _KEYS_RE.findall(stdout)
    total = int(keys[0]) if keys else None
    if not sections:
        if exit_code == 0:
            return DriftSummary("clean", 0, total, checked_at, detail="вывод без секций")
        return DriftSummary(
            "unknown", None, total, checked_at, detail="дрейф есть, вывод не разобран"
        )
    changed = sum(sections.values())
    if exit_code == 0 and changed == 0:
        return DriftSummary("clean", 0, total, checked_at)
    if changed > 0:
        return DriftSummary("drift", changed, total, checked_at)
    return DriftSummary("unknown", changed, total, checked_at, detail="код 1 при нуле расхождений")


def collect_drift(
    env: Environment, runner: CommandRunner, *, now: datetime
) -> tuple[DriftSummary, SourceObservation, tuple[Incident, ...]]:
    if env.drift_report is not None:
        summary, source = _drift_from_report(env.drift_report, now=now)
    elif env.drift_command:
        summary, source = _drift_from_command(env.drift_command, runner, now=now)
    else:
        summary = DriftSummary("unsupported", detail="источник дрейфа не настроен")
        source = SourceObservation("drift", "derived", "unsupported", detail=summary.detail)
        return summary, source, ()
    incidents: tuple[Incident, ...] = ()
    if summary.state == "drift":
        total = f" из {summary.total_keys}" if summary.total_keys is not None else ""
        incidents = (
            Incident(
                "config:drift", "warning", f"Дрейф конфига: {summary.changed_keys}{total} ключей"
            ),
        )
    return summary, source, incidents


def _drift_from_report(path: Path, *, now: datetime) -> tuple[DriftSummary, SourceObservation]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        summary = DriftSummary("unsupported", detail="файл отчёта отсутствует")
        return summary, SourceObservation("drift", "derived", "unsupported", detail=summary.detail)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        summary = DriftSummary("unknown", detail=f"отчёт нечитаем: {type(exc).__name__}")
        return summary, SourceObservation("drift", "derived", "unavailable", detail=summary.detail)
    if not isinstance(payload, dict):
        summary = DriftSummary("unknown", detail="отчёт не объект")
        return summary, SourceObservation("drift", "derived", "unavailable", detail=summary.detail)
    checked_at = payload.get("checked_at")
    checked_text = checked_at if isinstance(checked_at, str) else None
    exit_code = payload.get("exit_code")
    summary = parse_drift_output(
        str(payload.get("stdout") or ""),
        exit_code if isinstance(exit_code, int) else None,
        checked_at=checked_text,
    )
    freshness = classify_freshness(checked_text, now=now, ttl_seconds=DRIFT_STALE_SECONDS)
    return summary, SourceObservation("drift", "derived", freshness, observed_at=checked_text)


def _drift_from_command(
    argv: tuple[str, ...], runner: CommandRunner, *, now: datetime
) -> tuple[DriftSummary, SourceObservation]:
    result = runner.run(argv, timeout_seconds=30)
    checked = now.isoformat()
    if result.error is not None:
        summary = DriftSummary("unknown", checked_at=checked, detail=f"запуск: {result.error}")
        return summary, SourceObservation("drift", "derived", "unavailable", detail=summary.detail)
    summary = parse_drift_output(result.stdout, result.returncode, checked_at=checked)
    state: SourceState = "unavailable" if summary.state == "unknown" else "fresh"
    return summary, SourceObservation("drift", "derived", state, observed_at=checked)


# ----------------------------------------------------------------------------- limits
#
# The facade is called in-process, the way the gateway's own ``/usage`` calls it (see limits.py).
# ``collect_limits`` is the synchronous form for the cron/CLI path; ``collect_limits_async`` is
# the form a gateway plugin must use: the facade runs in a worker thread with a deadline so the
# event loop never waits on a provider API, and a miss is one ``unavailable`` tick, not a crash.

LIMITS_TIMEOUT_SECONDS = 45.0


def collect_limits(
    env: Environment, *, now: datetime, resolve: Resolver = import_fetcher
) -> tuple[CapacitySummary, SourceObservation, ProbeResult]:
    disabled = _limits_disabled(env)
    if disabled is not None:
        return disabled
    return parse_limits_payload(build_limits_payload(resolve), now=now)


async def collect_limits_async(
    env: Environment,
    *,
    now: datetime,
    resolve: Resolver = import_fetcher,
    timeout_seconds: float = LIMITS_TIMEOUT_SECONDS,
    flights: Flights | None = None,
) -> tuple[CapacitySummary, SourceObservation, ProbeResult]:
    disabled = _limits_disabled(env)
    if disabled is not None:
        return disabled
    payload = await fetch_limits_payload_off_loop(
        resolve, timeout_seconds=timeout_seconds, flights=flights
    )
    return parse_limits_payload(payload, now=now)


def _limits_disabled(
    env: Environment,
) -> tuple[CapacitySummary, SourceObservation, ProbeResult] | None:
    if env.limits_enabled:
        return None
    capacity = _capacity_unsupported("лимиты выключены в конфиге")
    source = SourceObservation("limits", "official", "unsupported", detail="выключено")
    return capacity, source, ProbeResult("limits", "unsupported", "disabled in config")


def parse_limits_payload(
    payload: dict[str, Any], *, now: datetime
) -> tuple[CapacitySummary, SourceObservation, ProbeResult]:
    if payload.get("ok") is not True:
        reason = sanitize_public_text(str(payload.get("reason") or "нет причины"), limit=80)
        if reason.startswith("unsupported"):
            capacity = _capacity_unsupported("фасад учёта недоступен на этой установке")
            source = SourceObservation("limits", "official", "unsupported", detail=reason)
            return capacity, source, ProbeResult("limits", "unsupported", reason)
        capacity = _capacity_unavailable(reason)
        source = SourceObservation("limits", "official", "unavailable", detail=reason)
        return capacity, source, ProbeResult("limits", "unknown", reason)

    quotas: dict[str, QuotaMetric] = {}
    fetched: list[str] = []
    for item in payload.get("providers") or ():
        if not isinstance(item, dict):
            continue
        key = str(item.get("provider") or "")
        label = _PROVIDER_LABELS.get(key)
        if label is None:
            continue
        quotas[key] = _quota_from_item(label, item)
        fetched_at = item.get("fetched_at")
        if quotas[key].kind == "official" and isinstance(fetched_at, str):
            fetched.append(fetched_at)
    ordered = [
        quotas.get(key) or QuotaMetric(label, "unavailable", detail="провайдер не в ответе")
        for key, label in _PROVIDER_LABELS.items()
    ]
    ordered.extend(
        QuotaMetric(label, "unsupported", detail="источник не подтверждён")
        for label in _UNCONFIRMED_PROVIDERS
    )
    observed_at = max(fetched) if fetched else None
    if observed_at is None:
        source = SourceObservation("limits", "official", "unavailable", detail="ни одного окна")
    else:
        freshness = classify_freshness(observed_at, now=now, ttl_seconds=LIMITS_STALE_SECONDS)
        source = SourceObservation("limits", "official", freshness, observed_at=observed_at)
    return CapacitySummary(tuple(ordered)), source, ProbeResult("limits", "supported")


def _quota_from_item(label: str, item: dict[str, Any]) -> QuotaMetric:
    if item.get("status") != "available":
        reason = item.get("reason")
        if reason == "none":
            detail = _NO_CREDENTIAL
        else:
            detail = sanitize_public_text(str(reason), limit=60) if reason else "нет данных"
        return QuotaMetric(label, "unavailable", detail=detail)
    windows: list[QuotaWindow] = []
    for raw in item.get("windows") or ():
        if not isinstance(raw, dict):
            continue
        used = raw.get("used_percent")
        used_value = float(used) if isinstance(used, (int, float)) and 0 <= used <= 100 else None
        reset = raw.get("reset_at")
        note = raw.get("note")
        windows.append(
            QuotaWindow(
                sanitize_public_text(str(raw.get("label") or "окно"), limit=24),
                used_value,
                reset if isinstance(reset, str) else None,
                note=sanitize_public_text(note, limit=24) if isinstance(note, str) else None,
            )
        )
    source = item.get("source")
    source_label = sanitize_public_text(str(source), limit=40) if source else None
    if not windows:
        return QuotaMetric(label, "unavailable", detail="ответ без окон")
    fetched_at = item.get("fetched_at")
    return QuotaMetric(
        label,
        "official",
        windows=tuple(windows),
        detail=source_label,
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
    )


# ----------------------------------------------------------------------------- Grok, Kimi
#
# Each provider read on its own cadence is one line and one source, with its own cache dict.


def collect_quota(
    key: str,
    label: str,
    cache: dict[str, Any],
    *,
    now: datetime,
    interval_seconds: float,
    fetch: QuotaFetch,
) -> tuple[QuotaMetric, SourceObservation]:
    """The provider's line and its own source (``<key>_quota``): one attempt per interval, the
    number never older than the interval (``quota_cache.tick``). ``cache`` is the caller's
    durable dict, mutated in place."""
    item = quota_tick(cache, now=now, interval_seconds=interval_seconds, fetch=fetch)
    metric = _quota_from_item(label, item)
    name = f"{key}_quota"
    if metric.kind != "official":
        source = SourceObservation(name, "official", "unavailable", detail=metric.detail)
    else:
        state = classify_freshness(
            metric.fetched_at, now=now, ttl_seconds=int(2 * interval_seconds)
        )
        source = SourceObservation(name, "official", state, observed_at=metric.fetched_at)
    return metric, source


def collect_grok(
    cache: dict[str, Any],
    *,
    now: datetime,
    interval_seconds: float = GROK_DEFAULT_INTERVAL_SECONDS,
    fetch: GrokFetch = grok_fetch_item,
) -> tuple[QuotaMetric, SourceObservation]:
    return collect_quota(
        "grok", GROK_LABEL, cache, now=now, interval_seconds=interval_seconds, fetch=fetch
    )


def collect_kimi(
    cache: dict[str, Any],
    *,
    now: datetime,
    interval_seconds: float = KIMI_DEFAULT_INTERVAL_SECONDS,
    fetch: KimiFetch = kimi_fetch_item,
) -> tuple[QuotaMetric, SourceObservation]:
    return collect_quota(
        "kimi", KIMI_LABEL, cache, now=now, interval_seconds=interval_seconds, fetch=fetch
    )


def quota_off_for(label: str, capacity: CapacitySummary) -> QuotaMetric:
    """A provider takes the block's verdict when the block itself is off or unsupported."""
    detail = capacity.quotas[0].detail if capacity.quotas else None
    return QuotaMetric(label, "unsupported", detail=detail or "лимиты выключены в конфиге")


def grok_off_for(capacity: CapacitySummary) -> QuotaMetric:
    return quota_off_for(GROK_LABEL, capacity)


def merge_quotas(capacity: CapacitySummary, *metrics: QuotaMetric) -> CapacitySummary:
    """Own-cadence providers sit after the facade providers and before the unconfirmed ones."""
    quotas = list(capacity.quotas)
    unconfirmed = [q for q in quotas if q.provider in _UNCONFIRMED_PROVIDERS]
    named = [q for q in quotas if q.provider not in _UNCONFIRMED_PROVIDERS]
    return CapacitySummary((*named, *metrics, *unconfirmed))


def merge_grok(capacity: CapacitySummary, metric: QuotaMetric) -> CapacitySummary:
    return merge_quotas(capacity, metric)


def _capacity_unavailable(detail: str) -> CapacitySummary:
    return CapacitySummary(
        tuple(
            QuotaMetric(label, "unavailable", detail=detail) for label in _PROVIDER_LABELS.values()
        )
        + tuple(
            QuotaMetric(label, "unsupported", detail="источник не подтверждён")
            for label in _UNCONFIRMED_PROVIDERS
        )
    )


def _capacity_unsupported(detail: str) -> CapacitySummary:
    return CapacitySummary(
        tuple(
            QuotaMetric(label, "unsupported", detail=detail)
            for label in (
                *_PROVIDER_LABELS.values(),
                GROK_LABEL,
                KIMI_LABEL,
                *_UNCONFIRMED_PROVIDERS,
            )
        )
    )


# ----------------------------------------------------------------------------- composition


def build_snapshot(
    *,
    now: datetime,
    gateway: GatewaySummary | None,
    drift: DriftSummary | None,
    capacity: CapacitySummary,
    sources: tuple[SourceObservation, ...],
    incidents: tuple[Incident, ...],
    coverage: Coverage | None = None,
    backup: BackupSummary | None = None,
) -> DashboardSnapshot:
    ordered = tuple(sorted(incidents, key=lambda item: _SEVERITY_RANK.get(item.severity, 9))[:5])
    cov = coverage or Coverage(expected_profiles=1, observed_profiles=1)
    overall = derive_overall(coverage=cov, sources=sources, incidents=ordered)
    return DashboardSnapshot(
        overall=overall,
        observed_at=now.isoformat(),
        coverage=cov,
        capacity=capacity,
        incidents=ordered,
        drift=drift,
        gateway=gateway,
        backup=backup,
        sources=sources,
    )


def collect_all(
    env: Environment,
    runner: CommandRunner,
    *,
    now: datetime,
    resolve_limits: Resolver = import_fetcher,
    grok_cache: dict[str, Any] | None = None,
    grok_interval_seconds: float = GROK_DEFAULT_INTERVAL_SECONDS,
    grok_fetch: GrokFetch | None = None,
    kimi_cache: dict[str, Any] | None = None,
    kimi_interval_seconds: float = KIMI_DEFAULT_INTERVAL_SECONDS,
    kimi_fetch: KimiFetch | None = None,
) -> DashboardSnapshot:
    gateway, gateway_source, gateway_incidents = collect_gateway(env, now=now)
    drift, drift_source, drift_incidents = collect_drift(env, runner, now=now)
    backup, backup_source, backup_incidents = collect_backup(env, now=now)
    capacity, limits_source, probe = collect_limits(env, now=now, resolve=resolve_limits)
    if probe.status == "supported":
        grok, grok_source = collect_grok(
            grok_cache if grok_cache is not None else {},
            now=now,
            interval_seconds=grok_interval_seconds,
            fetch=grok_fetch or grok_fetch_item,
        )
        kimi, kimi_source = collect_kimi(
            kimi_cache if kimi_cache is not None else {},
            now=now,
            interval_seconds=kimi_interval_seconds,
            fetch=kimi_fetch or kimi_fetch_item,
        )
    else:
        grok, grok_source = grok_off_for(capacity), _quota_off_source("grok", limits_source)
        kimi, kimi_source = (
            quota_off_for(KIMI_LABEL, capacity),
            _quota_off_source("kimi", limits_source),
        )
    return build_snapshot(
        now=now,
        gateway=gateway,
        drift=drift,
        backup=backup,
        capacity=merge_quotas(capacity, grok, kimi),
        sources=(
            gateway_source,
            limits_source,
            grok_source,
            kimi_source,
            drift_source,
            backup_source,
        ),
        incidents=(*gateway_incidents, *drift_incidents, *backup_incidents),
    )


def _quota_off_source(key: str, limits_source: SourceObservation) -> SourceObservation:
    return SourceObservation(
        f"{key}_quota", "official", limits_source.state, detail=limits_source.detail
    )


def _grok_off_source(limits_source: SourceObservation) -> SourceObservation:
    return _quota_off_source("grok", limits_source)


# ----------------------------------------------------------------------------- plugin tick
#
# Inside the gateway the tick shares the event loop with every Telegram update. The drift command
# and the usage facade therefore run in worker threads with deadlines, concurrently; the gateway
# file is one small read. A collector that raises something its own handlers did not foresee is a
# bug in the dashboard, and the screen says so: that source is ``unavailable`` with a warning
# incident, the other sources keep their answers, and the tick still edits the message.

# The runner's own hard limit is 30 s; the slack covers thread start-up and process teardown.
DRIFT_TIMEOUT_SECONDS = 35.0
# Guards catch BaseException, not Exception, so a SystemExit raised in the same task degrades the
# source; one raised in a worker thread is converted at the thread boundary (``workers.py``),
# because the Task running ``to_thread`` would re-raise it out of the loop before any ``except``
# here. Cancellation and the operator's interrupt are the two that must keep propagating.
_UNGUARDED = (asyncio.CancelledError, KeyboardInterrupt)

GatewayPart = tuple[GatewaySummary, SourceObservation, tuple[Incident, ...]]
DriftPart = tuple[DriftSummary, SourceObservation, tuple[Incident, ...]]
LimitsPart = tuple[CapacitySummary, SourceObservation, tuple[Incident, ...]]


async def collect_all_async(
    env: Environment,
    runner: CommandRunner,
    *,
    now: datetime,
    resolve_limits: Resolver = import_fetcher,
    limits_timeout_seconds: float = LIMITS_TIMEOUT_SECONDS,
    drift_timeout_seconds: float = DRIFT_TIMEOUT_SECONDS,
    flights: Flights | None = None,
    grok_cache: dict[str, Any] | None = None,
    grok_interval_seconds: float = GROK_DEFAULT_INTERVAL_SECONDS,
    grok_timeout_seconds: float = GROK_TICK_TIMEOUT_SECONDS,
    grok_fetch: GrokFetch | None = None,
    kimi_cache: dict[str, Any] | None = None,
    kimi_interval_seconds: float = KIMI_DEFAULT_INTERVAL_SECONDS,
    kimi_timeout_seconds: float = KIMI_TICK_TIMEOUT_SECONDS,
    kimi_fetch: KimiFetch | None = None,
) -> DashboardSnapshot:
    """``collect_all`` for a tick that runs on the gateway's event loop. Never raises.

    ``flights`` shared across ticks keeps one worker per source: a source abandoned by its
    deadline is not started again until it returns. ``grok_cache`` and ``kimi_cache`` are the
    caller's durable dicts (the plugin keeps them in its record, one per provider): a worker
    abandoned by its deadline still writes its attempt there when it returns, so the next tick
    serves it instead of asking again.
    """
    flights = flights or Flights()
    gateway, gateway_source, gateway_incidents = _gateway_guarded(env, now=now)
    backup, backup_source, backup_incidents = _backup_guarded(env, now=now)
    (
        (drift, drift_source, drift_incidents),
        (capacity, limits_source, limits_incidents),
    ) = await asyncio.gather(
        _drift_off_loop(
            env, runner, now=now, timeout_seconds=drift_timeout_seconds, flights=flights
        ),
        _limits_guarded(
            env,
            now=now,
            resolve=resolve_limits,
            timeout_seconds=limits_timeout_seconds,
            flights=flights,
        ),
    )
    grok_incidents: tuple[Incident, ...] = ()
    kimi_incidents: tuple[Incident, ...] = ()
    if env.limits_enabled and limits_source.state != "unsupported":
        (
            (grok, grok_source, grok_incidents),
            (kimi, kimi_source, kimi_incidents),
        ) = await asyncio.gather(
            _quota_guarded(
                "grok",
                GROK_LABEL,
                grok_cache if grok_cache is not None else {},
                now=now,
                interval_seconds=grok_interval_seconds,
                timeout_seconds=grok_timeout_seconds,
                flights=flights,
                fetch=grok_fetch or grok_fetch_item,
            ),
            _quota_guarded(
                "kimi",
                KIMI_LABEL,
                kimi_cache if kimi_cache is not None else {},
                now=now,
                interval_seconds=kimi_interval_seconds,
                timeout_seconds=kimi_timeout_seconds,
                flights=flights,
                fetch=kimi_fetch or kimi_fetch_item,
            ),
        )
    else:
        grok, grok_source = grok_off_for(capacity), _quota_off_source("grok", limits_source)
        kimi, kimi_source = (
            quota_off_for(KIMI_LABEL, capacity),
            _quota_off_source("kimi", limits_source),
        )
    return build_snapshot(
        now=now,
        gateway=gateway,
        drift=drift,
        backup=backup,
        capacity=merge_quotas(capacity, grok, kimi),
        sources=(
            gateway_source,
            limits_source,
            grok_source,
            kimi_source,
            drift_source,
            backup_source,
        ),
        incidents=(
            *gateway_incidents,
            *drift_incidents,
            *backup_incidents,
            *limits_incidents,
            *grok_incidents,
            *kimi_incidents,
        ),
    )


def _gateway_guarded(env: Environment, *, now: datetime) -> GatewayPart:
    try:
        return collect_gateway(env, now=now)
    except _UNGUARDED:
        raise
    except BaseException as exc:
        source, incident = _collector_crashed("gateway_state", "official", exc)
        return GatewaySummary("unknown", "unknown", detail=source.detail), source, (incident,)


def _backup_guarded(env: Environment, *, now: datetime) -> BackupPart:
    """One small file read; a surprise while reading it is one unavailable source, not a dead
    tick."""
    try:
        return collect_backup(env, now=now)
    except _UNGUARDED:
        raise
    except BaseException as exc:
        source, incident = _collector_crashed("backup", "derived", exc)
        return BackupSummary("unknown", detail=source.detail), source, (incident,)


async def _drift_off_loop(
    env: Environment,
    runner: CommandRunner,
    *,
    now: datetime,
    timeout_seconds: float,
    flights: Flights,
) -> DriftPart:
    try:
        return await flights.run(
            "drift", collect_drift, env, runner, now=now, timeout_seconds=timeout_seconds
        )
    except TimeoutError:
        # The worker thread finishes on its own; the runner kills the process tree at its limit.
        # No checked_at: a check that did not finish must not render as "проверено HH:MM".
        return _drift_not_collected(f"дедлайн {timeout_seconds:g} с")
    except StillRunning:
        return _drift_not_collected("предыдущий запрос ещё выполняется")
    except _UNGUARDED:
        raise
    except BaseException as exc:
        source, incident = _collector_crashed("drift", "derived", exc)
        return DriftSummary("unknown", detail=source.detail), source, (incident,)


def _drift_not_collected(detail: str) -> DriftPart:
    summary = DriftSummary("unknown", detail=detail)
    return summary, SourceObservation("drift", "derived", "unavailable", detail=detail), ()


async def _limits_guarded(
    env: Environment,
    *,
    now: datetime,
    resolve: Resolver,
    timeout_seconds: float,
    flights: Flights,
) -> LimitsPart:
    try:
        capacity, source, _probe = await collect_limits_async(
            env, now=now, resolve=resolve, timeout_seconds=timeout_seconds, flights=flights
        )
        return capacity, source, ()
    except _UNGUARDED:
        raise
    except BaseException as exc:
        source, incident = _collector_crashed("limits", "official", exc)
        return _capacity_unavailable(source.detail or "сборщик упал"), source, (incident,)


QuotaPart = tuple[QuotaMetric, SourceObservation, tuple[Incident, ...]]


async def _quota_guarded(
    key: str,
    label: str,
    cache: dict[str, Any],
    *,
    now: datetime,
    interval_seconds: float,
    timeout_seconds: float,
    flights: Flights,
    fetch: QuotaFetch,
) -> QuotaPart:
    """``collect_quota`` in a worker with a deadline; a deadline or a busy worker is one tick of
    "нет данных" with the reason, the cache untouched until the worker returns."""
    try:
        metric, source = await flights.run(
            key,
            collect_quota,
            key,
            label,
            cache,
            now=now,
            interval_seconds=interval_seconds,
            fetch=fetch,
            timeout_seconds=timeout_seconds,
        )
        return metric, source, ()
    except TimeoutError:
        return (*_quota_unavailable(key, label, f"не ответил за {timeout_seconds:g} с"), ())
    except StillRunning:
        return (*_quota_unavailable(key, label, "предыдущий запрос ещё не вернулся"), ())
    except _UNGUARDED:
        raise
    except BaseException as exc:
        source, incident = _collector_crashed(f"{key}_quota", "official", exc)
        return QuotaMetric(label, "unavailable", detail=source.detail), source, (incident,)


def _quota_unavailable(key: str, label: str, detail: str) -> tuple[QuotaMetric, SourceObservation]:
    return (
        QuotaMetric(label, "unavailable", detail=detail),
        SourceObservation(f"{key}_quota", "official", "unavailable", detail=detail),
    )


def _collector_crashed(
    name: str, authority: Authority, exc: BaseException
) -> tuple[SourceObservation, Incident]:
    """An exception the collector did not foresee: class name only, the text may carry paths."""
    reason = failure_name(exc)
    logger.exception("collector %s crashed; source marked unavailable", name)
    source = SourceObservation(name, authority, "unavailable", detail=f"сборщик упал: {reason}")
    incident = Incident(f"collector:{name}", "warning", f"Сборщик {name} упал ({reason})")
    return source, incident
