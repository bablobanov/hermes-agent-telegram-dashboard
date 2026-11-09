"""The free space of the partition the profile home lies on (1.1.1).

One read, ``shutil.disk_usage`` of ``HERMES_HOME`` (``statvfs`` on Linux): nothing is written,
no command runs, no file is opened. The free space is what the gateway's user may still write,
not the blocks only root may take. The line is shown always, in the details: ``Disk: 61 GB free
of 80``. Under either floor of the owner's settings, a share of the partition
(``disk_low_percent``, 10 by default) or an amount (``disk_low_gb``, 2 by default), the disk is
low: the line takes the warning mark and an event names it under "Needs attention".

Not a source: the coverage counts what the dashboard reads of Hermes, and the partition is the
host's. A partition that cannot be read is a reason in the details, never a zero and never an
alarm. GB here is 10^9 bytes.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .compat import Environment
from .schema import DiskSummary, Incident

GB = 1_000_000_000
INCIDENT_ID = "disk:low"
# ``shutil.disk_usage``: ``total``, ``used`` and ``free`` of the partition a path lies on.
Usage = Callable[[Path], Any]
DiskPart = tuple[DiskSummary, tuple[Incident, ...]]


def read_disk(env: Environment, *, usage: Usage = shutil.disk_usage) -> DiskPart:
    """The partition of ``env.hermes_home`` against the floors, and the event when it is low."""
    try:
        answer = usage(env.hermes_home)
        total, free = int(answer.total), int(answer.free)
    except OSError as exc:
        return DiskSummary("unknown", detail=f"not read ({type(exc).__name__})"), ()
    if total <= 0 or not 0 <= free <= total:
        return DiskSummary("unknown", detail="the partition reports no size"), ()
    low = free * 100 < total * env.disk_low_percent or free < env.disk_low_gb * GB
    summary = DiskSummary("low" if low else "ok", free_bytes=free, total_bytes=total)
    if not low:
        return summary, ()
    title = f"Disk: {space_words(free, total)} ({free * 100 // total}%)"
    return summary, (Incident(INCIDENT_ID, "warning", title),)


def space_words(free: int, total: int) -> str:
    """``61 GB free of 80``: the two sizes, the unit said once. The free figure is rounded down,
    so a disk under the 2 GB floor never reads ``2.0 GB free``."""
    return f"{gigabytes(free, down=True)} GB free of {gigabytes(total)}"


def gigabytes(size: int, *, down: bool = False) -> str:
    """A size in GB: one decimal under 10 (``1.6``), whole above it (``61``, ``1,024``);
    rounded to the nearest, or ``down``."""
    value = size / GB
    if down:
        tenths = math.floor(value * 10) / 10
        return f"{tenths:.1f}" if tenths < 10 else f"{math.floor(value):,}"
    if round(value, 1) < 10:
        return f"{value:.1f}"
    return f"{value:,.0f}"
