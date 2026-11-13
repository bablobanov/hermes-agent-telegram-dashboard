"""The widget's packets, each at most 1024 bytes of UTF-8 (Bot API 10.4):

- ``chat_data`` (Status): the status, the headline, the Status blocks in the dashboard's words
  (its emoji and list marks off: the widget draws its own; the Hermes version keeps its 🤖), the
  limits as ready cards, the disk, the theme, the collection's time and the tick's number.
  Rewritten on every tick
- ``public_data`` (Cron): the failing jobs, every group by frequency with its soonest run, the
  nearest groups open with their jobs' whole names, the paused and the done as counts. Sent with
  the message and with an edit when it changes
- the answer to ``details`` (Details): the Details groups but the cron jobs, on a tap only
- the answer to ``cron:<frequency>``, ``cron:paused`` or ``cron:done``: a folded group's jobs,
  on a tap only

A screen longer than a packet is cut by a fixed rule, never refused; ``cut`` tells the widget.
"""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime, tzinfo
from typing import Any

from .view import CronView, View, unchecked_items

PACKET_LIMIT = 1024
LINE_LIMIT = 120
HEADLINE_LIMIT = 60
CRON_NAME_LIMIT = 16  # the shortest a job's name gets in a tap's answer over the limit
CRON_ROWS = 8  # the jobs the Cron tab opens at most: with the headers it stays within 580 px
STALE = "Stale data"
UNCHECKED_TITLE = "Not checked"
# The interface's words, in the order widget.html reads them (``X[0]`` … ``X[16]``): they ride in
# the Cron packet instead of the HTML, whose zlib bytes are the scarcer (the owner, 13.11); the
# Status packet has no room for them beside a palette.
UI_WORDS = (
    "Refresh",
    "Sending…",
    "Collecting…",
    "Loading…",
    "… trimmed to fit",
    "Not sent: ",
    "Bot: ",
    " · tick ",
    "Disk",
    "⚠ Failing · ",
    "Paused",
    "Done",
    " more",
    "Status",
    "Cron",
    "Details",
    "Theme: ",
)
# What goes first when the Status packet is over the limit: the least important block first.
DROP_ORDER = ("work", "unchecked", "hermes", "works", "note", "attention")
TITLES = {
    "limits": "Limits",
    "telegram": "Telegram",
    "backup": "Backup & drift",
    "cron": "Cron",
    "hermes": "Hermes",
    "rules": "Rules",
    "memory": "Memory",
    "disk": "Disk",
    "no data": "No data",
}
LEAD = re.compile(r"^[\W_]+")  # the dashboard's emoji and list marks before a line's words
SPACES = " \u00a0"

Packet = dict[str, Any]

# ----------------------------------------------------------------------------- themes

DEFAULT_THEME = "telegram"
# Palettes for Telegram's dark theme; the light theme and ``telegram`` keep the chat's own
# colours. Six base colours each (midnight, ember and mono by Anton; neon from the owner's
# reference of 10.11), warn and crit chosen to differ from the accent and the bars in hue and to
# keep 3:1 to the bar's track (the tests check both). Optional: ``bar`` (default the accent),
# ``val`` (the values; the text), ``bar2`` (the bar's far end, a gradient; the bar), ``alw`` (the
# "Needs attention" card; warn over the card at 20%).
PALETTES: dict[str, dict[str, str]] = {
    "midnight": {
        "bg": "0f121e",
        "card": "191d2e",
        "line": "31364c",
        "ink": "ebeaf5",
        "mut": "a4a9c0",
        "acc": "a69beb",
        "warn": "f2b84b",
        "crit": "f47a80",
    },
    "ember": {
        "bg": "191311",
        "card": "271d19",
        "line": "49352a",
        "ink": "f5e3d2",
        "mut": "bca390",
        "acc": "cd965e",
        "warn": "e9e05c",
        "crit": "f2667d",
        "alw": "4e3819",  # warm amber (#e8a317 at 20%): the lemon warn over brown reads olive
    },
    "mono": {
        "bg": "121414",
        "card": "1c1f1e",
        "line": "353b38",
        "ink": "e8e9e5",
        "mut": "a6aea8",
        "acc": "c7d1c9",
        "warn": "e8b54a",
        "crit": "ef6f6c",
    },
    "neon": {
        "bg": "1c1028",
        "card": "2a1836",
        "line": "43304f",
        "ink": "f6eeff",
        "mut": "b8a8c8",
        "acc": "f550b4",
        "warn": "ffd60a",
        "crit": "ff6a3d",
        "bar": "a068f0",
        "val": "48dce6",
        "bar2": "f550b4",
    },
}
THEMES = (DEFAULT_THEME, *PALETTES)
# The order the widget reads a palette in: six hex digits each.
TOKENS = (
    "bg",
    "card",
    "line",
    "ink",
    "mut",
    "acc",
    "on",
    "warn",
    "crit",
    "trk",
    "alw",
    "alc",
    "bar",
    "val",
    "bar2",
)


def mix(a: str, b: str, share: float) -> str:
    """``a`` over ``b`` at ``share``, channel by channel in sRGB (``color-mix(in srgb …)``)."""
    channels = (
        round(int(a[i : i + 2], 16) * share + int(b[i : i + 2], 16) * (1 - share))
        for i in (0, 2, 4)
    )
    return "".join(f"{c:02x}" for c in channels)


def colours(name: str) -> dict[str, str] | None:
    """All the tokens of a palette: the button text is the background, the bar's track the text
    over the card at 10%, the tinted cards warn and crit over the card at 20%."""
    base = PALETTES.get(name)
    if base is None:
        return None
    out = dict(base)
    out.setdefault("on", base["bg"])
    out.setdefault("trk", mix(base["ink"], base["card"], 0.10))
    out.setdefault("alw", mix(base["warn"], base["card"], 0.20))
    out.setdefault("alc", mix(base["crit"], base["card"], 0.20))
    out.setdefault("bar", base["acc"])
    out.setdefault("val", base["ink"])
    out.setdefault("bar2", out["bar"])
    return out


def next_theme(name: str) -> str:
    """The theme after ``name`` in ``THEMES``, round: what the widget's theme button asks for."""
    return THEMES[(THEMES.index(name) + 1) % len(THEMES)] if name in THEMES else DEFAULT_THEME


def palette(name: str) -> str | None:
    """The palette as the widget sets it on its body: CSS custom properties, one per token
    (``--bg:#1c1028;--card:…``); ``None`` for the chat's own colours."""
    found = colours(name)
    return None if found is None else ";".join(f"--{token}:#{found[token]}" for token in TOKENS)


# ----------------------------------------------------------------------------- words


def words(key: str, lines: tuple[str, ...] | list[str]) -> list[str]:
    """A block's lines without the dashboard's emoji and list marks: ``works`` one word per
    check; ``unchecked`` a card of its items under its title (2A, 11.11); a title keeps its
    ``## ``; the Hermes version keeps the dashboard's 🤖 (12.11)."""
    if key == "works":
        return [w.strip(SPACES) for w in " ".join(lines).split("✓") if w.strip(SPACES)]
    if key == "hermes":
        return [line.strip(SPACES) for line in lines]
    if key == "unchecked":
        return [f"## {UNCHECKED_TITLE}", *(i for line in lines for i in unchecked_items(line))]
    return [
        "## " + LEAD.sub("", line[3:]) if line.startswith("## ") else LEAD.sub("", line)
        for line in lines
    ]


def clock_words(at: int, zone: tzinfo) -> tuple[str, str]:
    """A collection's time (Unix ms) as the topic writes it in its zone: ``22:12``, ``Nov 11``."""
    moment = datetime.fromtimestamp(at / 1000, zone)
    return f"{moment:%H:%M}", f"{moment:%b} {moment.day}"


# ----------------------------------------------------------------------------- the packets


def chat_packet(
    tick: int, at: int, view: View, *, stale: bool = False, theme: str = DEFAULT_THEME
) -> Packet:
    """The tick's packet. ``at`` is the collection's Unix time in ms (the widget keeps the newest
    packet by it), ``w`` and ``d`` its clock and day in the dashboard's zone (the header shows
    the day only when the collection is not of the device's today); ``dk`` is ``[words, % used,
    severity]``, absent when the collector read no disk. A stale screen is unknown with the
    headline "Stale data". ``th`` names the theme, ``pl`` carries its colours, ``nx`` names the
    theme the button asks for next."""
    clock, day = clock_words(at, view.zone)
    chosen = theme if theme in THEMES else DEFAULT_THEME
    packet: Packet = {
        "v": 5,
        "n": tick,
        "t": at,
        "w": clock,
        "d": day,
        "st": view.status,
        "h": view.headline,
        "m": [[key, words(key, lines)] for key, lines in view.blocks],
        "q": copy.deepcopy(list(view.providers)),
        "th": chosen,
        "nx": next_theme(chosen),
    }
    colours_text = palette(chosen)
    if colours_text:
        packet["pl"] = colours_text
    if stale:
        packet["st"], packet["h"] = "unknown", STALE
    if view.disk is not None:
        packet["dk"] = list(view.disk)
    return fit_chat(packet)


def nearest(rows: tuple[list[Any], ...] | list[list[Any]]) -> float:
    """A group's soonest next run: its first job's (they are in order), unknown last."""
    return rows[0][1] if rows and rows[0][1] is not None else math.inf


def cron_packet(view: View, at: int) -> Packet:
    """The Cron tab: the failing jobs, every group's header (frequency, count, its soonest run in
    words), the nearest groups open with their jobs' whole names while ``CRON_ROWS`` and 1024
    bytes allow (from the soonest run; of two with the same, the smaller first; the first that
    does not fit stops the opening), the paused and the done as counts. The rest comes on a tap
    (``cron_answer``). ``t`` is the collection's time."""
    cron = view.cron or CronView("No data")
    packet: Packet = {"v": 5, "t": at, "h": cron.head, "x": list(UI_WORDS)}
    if cron.failing:
        packet["a"] = [list(row) for row in cron.failing]
    groups = cron.groups
    if groups:
        packet["g"] = [[label, len(rows), rows[0][2] if rows else ""] for label, rows in groups]
    if cron.paused:
        packet["p"] = len(cron.paused)
    if cron.done:
        packet["d"] = len(cron.done)
    left = CRON_ROWS
    order = sorted(range(len(groups)), key=lambda i: (nearest(groups[i][1]), len(groups[i][1])))
    for index in order:
        rows = groups[index][1]
        if not rows or len(rows) > left:
            break
        packet["g"][index].append([[name, when] for name, _, when in rows])
        if size(packet) > PACKET_LIMIT:
            packet["g"][index].pop()
            break
        left -= len(rows)
    return fit_cron(packet)


def cron_answer(view: View, key: str) -> Packet:
    """A tap on a folded group (its frequency), on Paused or on Done: the jobs' whole names, a
    group's with the next run, a paused job's with the day of its next run, a done one's with its
    last, from the last collection. Past 1024 bytes the names get shorter, down to
    ``CRON_NAME_LIMIT``, then the last jobs go, with ``cut``; ``gone`` when the group is gone."""
    cron = view.cron or CronView("")
    rows: list[list[Any]]
    if key in ("paused", "done"):
        rows = [list(row) for row in (cron.paused if key == "paused" else cron.done)]
    else:
        group = next((rows for label, rows in cron.groups if label == key), None)
        if group is None:
            return {"e": "gone"}
        rows = [[name, when] for name, _, when in group]
    packet: Packet = {"v": 5, "r": rows}
    limit = max((len(row[0]) for row in rows), default=0)
    while size(packet) > PACKET_LIMIT and limit > CRON_NAME_LIMIT:
        limit = max(limit - 4, CRON_NAME_LIMIT)
        packet["r"] = [[clip(row[0], limit), *row[1:]] for row in rows]
    while size(packet) > PACKET_LIMIT and packet["r"]:
        packet["cut"] = 1
        packet["r"].pop()
    return packet


def details_packet(view: View) -> Packet:
    groups = [[TITLES.get(key, key.capitalize()), list(lines)] for key, lines in view.details]
    return fit_public({"v": 5, "d": groups})


# ----------------------------------------------------------------------------- the limit


def text_of(packet: Packet) -> str:
    """Compact JSON; a lone surrogate (an escaped half of an emoji in a job's name) becomes
    ``?``, so the text is valid UTF-8 and the tick never stops on it."""
    text = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "replace").decode("utf-8")


def size(packet: Packet) -> int:
    return len(text_of(packet).encode("utf-8"))


def clip(line: str, limit: int = LINE_LIMIT) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _block(packet: Packet, key: str) -> list[str]:
    found: list[str] = next((lines for name, lines in packet["m"] if name == key), [])
    return found


def fit_chat(packet: Packet) -> Packet:
    if size(packet) <= PACKET_LIMIT:
        return packet
    packet["cut"] = 1
    packet["m"] = [[key, [clip(line) for line in lines]] for key, lines in packet["m"]]
    for key in DROP_ORDER:  # lines go from the end; a card's title stays past the limits
        lines = _block(packet, key)
        keep = 1 if key in ("attention", "unchecked") else 0
        while size(packet) > PACKET_LIMIT and len(lines) > keep:
            lines.pop()
    packet["m"] = [[key, lines] for key, lines in packet["m"] if lines]
    while size(packet) > PACKET_LIMIT and packet["q"]:
        packet["q"].pop()
    for _key, lines in packet["m"]:
        while size(packet) > PACKET_LIMIT and lines:
            lines.pop()
    packet["m"] = [[key, lines] for key, lines in packet["m"] if lines]
    if size(packet) > PACKET_LIMIT:  # only a headline beyond any screen is left
        packet["h"], packet["m"], packet["q"] = clip(str(packet["h"]), HEADLINE_LIMIT), [], []
        packet.pop("pl", None)
    return packet


def fit_cron(packet: Packet) -> Packet:
    """Over 1024 bytes with every group folded (a long failing list): the failing jobs' notes go,
    then the groups from the least frequent, then the failing jobs from the end, with ``cut``; at
    worst only the summary is left."""
    for failing in packet.get("a", []):
        if size(packet) > PACKET_LIMIT:
            failing[2] = ""
    groups = packet.get("g", [])
    while size(packet) > PACKET_LIMIT and groups:
        packet["cut"] = 1
        groups.pop()
    while size(packet) > PACKET_LIMIT and packet.get("a"):
        packet["cut"] = 1
        packet["a"].pop()
    if size(packet) > PACKET_LIMIT:  # only the summary is left
        head = clip(str(packet.get("h", "")), HEADLINE_LIMIT)
        packet = {"v": packet["v"], "t": packet["t"], "h": head, "x": packet["x"], "cut": 1}
    return packet


def fit_public(packet: Packet) -> Packet:
    if size(packet) <= PACKET_LIMIT:
        return packet
    packet["cut"] = 1
    groups: list[list[Any]] = [
        [clip(title, HEADLINE_LIMIT), [clip(line) for line in lines]]
        for title, lines in packet["d"]
    ]
    packet["d"] = groups
    while size(packet) > PACKET_LIMIT and any(lines for _, lines in groups):
        longest = max((group for group in groups if group[1]), key=lambda g: size({"g": g}))
        longest[1].pop()
    packet["d"] = [group for group in groups if group[1]]
    while size(packet) > PACKET_LIMIT and packet["d"]:
        packet["d"].pop()
    return packet


def encode(packet: Packet) -> str:
    """Compact JSON; raises ``ValueError`` past the 1024-byte limit, so a packet Telegram would
    refuse is never sent."""
    text = text_of(packet)
    if len(text.encode("utf-8")) > PACKET_LIMIT:
        raise ValueError("widget packet over 1024 bytes")
    return text
