"""``widget.packets``: the widget's packets (at most 1024 bytes each), words and palettes."""

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from telegram_dashboard import render
from telegram_dashboard.states import all_states
from telegram_dashboard.widget import packets as pk
from telegram_dashboard.widget import view as wv

ZONE = timezone(timedelta(hours=5), "+05")
AT = int(datetime(2026, 11, 11, 17, 12, tzinfo=UTC).timestamp() * 1000)  # 22:12 in +05
LIMIT = pk.PACKET_LIMIT


def a_view(**fields) -> wv.View:
    base: dict = {
        "status": "normal",
        "headline": "Healthy",
        "blocks": (
            ("works", ("✓\xa0Gateway ✓\xa0Telegram ✓\xa0Backup",)),
            ("hermes", ("🤖 Hermes 0.22.2",)),
        ),
        "details": (("telegram", ("✓ Telegram polling ok 20:58",)),),
        "providers": (["Grok", None, "no data"],),
        "zone": ZONE,
    }
    base.update(fields)
    return wv.View(**base)


def rows(*items: tuple[str, int | None, str]) -> tuple[list, ...]:
    return tuple([name, minutes, when] for name, minutes, when in items)


def cron_view(*groups: tuple[str, tuple[list, ...]], **fields) -> wv.View:
    cron = wv.CronView("5 active · ticker 20:58", groups=tuple(groups), **fields)
    return a_view(cron=cron)


def opened(packet: dict) -> list[str]:
    return [group[0] for group in packet.get("g", []) if len(group) == 4]


# ----------------------------------------------------------------------------- the demo states


def every_screen():
    for state in all_states():
        parts = render.render_parts(
            state.snapshot, now=state.now, delivery=None, period_seconds=300, zone=ZONE
        )
        yield state.number, wv.view(state.snapshot, parts, state.now, ZONE)


@pytest.mark.parametrize("theme", ["telegram", "neon"])
def test_every_demo_state_fits_each_packet_whole_without_a_cut(theme):
    worst = {"chat": (0, 0), "cron": (0, 0), "details": (0, 0)}
    cut: list[str] = []
    for number, screen in every_screen():
        made = {
            "chat": pk.chat_packet(7, AT, screen, theme=theme),
            "cron": pk.cron_packet(screen, AT),
            "details": pk.details_packet(screen),
        }
        for kind, packet in made.items():
            used = pk.size(packet)
            worst[kind] = max(worst[kind], (used, number))
            assert used <= LIMIT, f"{kind} of state {number} is {used} bytes"
            if "cut" in packet:
                cut.append(f"{kind} of state {number}")
        pk.encode(made["chat"])
    assert not cut, f"cut: {cut}; worst (bytes, state): {worst}"
    print(f"worst sizes for {theme}: {worst}")


def test_the_demo_states_are_thirty():
    assert len(list(every_screen())) == 30


# ----------------------------------------------------------------------------- chat packet


def test_the_chat_packet_has_the_keys_of_version_five():
    packet = pk.chat_packet(7, AT, a_view())
    assert set(packet) == {"v", "n", "t", "w", "d", "st", "h", "m", "q", "th", "nx"}
    assert (packet["v"], packet["n"], packet["t"]) == (5, 7, AT)
    assert (packet["st"], packet["h"]) == ("normal", "Healthy")
    assert packet["q"] == [["Grok", None, "no data"]]


def test_the_clock_and_day_are_the_dashboards_zone_not_utc():
    packet = pk.chat_packet(1, AT, a_view())
    assert (packet["w"], packet["d"]) == ("22:12", "Nov 11")
    assert pk.clock_words(AT, UTC) == ("17:12", "Nov 11")


def test_the_day_is_not_zero_padded():
    at = int(datetime(2026, 11, 3, 1, 0, tzinfo=UTC).timestamp() * 1000)
    assert pk.clock_words(at, UTC) == ("01:00", "Nov 3")


def test_the_default_theme_has_no_palette_and_asks_for_the_next_one():
    packet = pk.chat_packet(1, AT, a_view())
    assert packet["th"] == "telegram"
    assert packet["nx"] == pk.THEMES[1]
    assert "pl" not in packet


@pytest.mark.parametrize("theme", list(pk.PALETTES))
def test_a_palette_theme_carries_its_colours_and_names_the_next_theme(theme):
    packet = pk.chat_packet(1, AT, a_view(), theme=theme)
    assert packet["th"] == theme
    assert packet["pl"] == pk.palette(theme)
    assert packet["nx"] == pk.next_theme(theme)


def test_next_theme_cycles_through_every_theme_and_returns_to_the_start():
    seen, name = [], pk.DEFAULT_THEME
    for _ in pk.THEMES:
        seen.append(name)
        name = pk.next_theme(name)
    assert seen == list(pk.THEMES)
    assert name == pk.DEFAULT_THEME


def test_an_unknown_theme_name_is_the_default_one():
    assert pk.next_theme("pink") == pk.DEFAULT_THEME
    packet = pk.chat_packet(1, AT, a_view(), theme="pink")
    assert (packet["th"], "pl" in packet) == (pk.DEFAULT_THEME, False)


def test_a_stale_screen_is_unknown_with_the_stale_headline():
    packet = pk.chat_packet(1, AT, a_view(status="critical"), stale=True)
    assert (packet["st"], packet["h"]) == ("unknown", "Stale data")


def test_the_disk_is_copied_from_the_view_and_absent_without_one():
    assert pk.chat_packet(1, AT, a_view(disk=["61 GB free of 80", 24, 0]))["dk"] == [
        "61 GB free of 80",
        24,
        0,
    ]
    assert "dk" not in pk.chat_packet(1, AT, a_view())


def test_the_providers_are_copied_not_shared_with_the_view():
    screen = a_view()
    packet = pk.chat_packet(1, AT, screen)
    packet["q"][0].append("changed")
    assert screen.providers[0] == ["Grok", None, "no data"]


# ----------------------------------------------------------------------------- words


def test_works_is_one_word_per_check_with_the_non_breaking_spaces_stripped():
    lines = ("✓\xa0Gateway ✓\xa0Telegram polling ✓\xa0Backup",)
    assert pk.words("works", lines) == ["Gateway", "Telegram polling", "Backup"]


def test_works_joins_its_lines_before_splitting():
    assert pk.words("works", ("✓ One", "✓ Two ✓ Three")) == ["One", "Two", "Three"]


def test_hermes_keeps_its_robot():
    assert pk.words("hermes", ("🤖 Hermes 0.22.2\xa0",)) == ["🤖 Hermes 0.22.2"]


def test_unchecked_is_a_titled_card_of_its_items():
    lines = ("❔ Not checked: Gemini log (not on this installation, x), profiles 2/3",)
    assert pk.words("unchecked", lines) == [
        "## Not checked",
        "Gemini log (not on this installation, x)",
        "profiles 2/3",
    ]


def test_other_blocks_lose_their_leading_emoji_and_marks_and_keep_titles():
    lines = ("## 🔴 Needs attention", "⚠️ Backup failed", "• Disk low", "plain")
    assert pk.words("attention", lines) == [
        "## Needs attention",
        "Backup failed",
        "Disk low",
        "plain",
    ]


# ----------------------------------------------------------------------------- the limit


def lines_of(count: int, size: int = 100, tag: str = "x") -> list[str]:
    return [f"{tag}{i} " + "w" * (size - 4) for i in range(count)]


def a_packet(**blocks: list[str]) -> dict:
    packet = pk.chat_packet(1, AT, a_view(blocks=()))
    packet["m"] = [[key, lines] for key, lines in blocks.items()]
    return packet


def test_a_packet_within_the_limit_is_returned_as_it_is():
    packet = a_packet(works=["Gateway"])
    assert pk.fit_chat(packet) is packet
    assert "cut" not in packet


def test_work_goes_first_when_the_packet_is_over():
    blocks = {
        "attention": ["## Needs attention", *lines_of(2, tag="a")],
        "works": lines_of(2, tag="k"),
        "hermes": ["🤖 Hermes"],
        "work": lines_of(12, tag="w"),
    }
    fitted = pk.fit_chat(a_packet(**blocks))
    got = dict(fitted["m"])
    assert fitted["cut"] == 1
    assert 0 < len(got["work"]) < 12
    assert got["attention"] == blocks["attention"]
    assert got["works"] == blocks["works"]
    assert pk.size(fitted) <= LIMIT


def test_unchecked_follows_work_and_keeps_its_title_line():
    blocks = {
        "attention": ["## Needs attention", "one"],
        "unchecked": ["## Not checked", *lines_of(14, tag="u")],
        "hermes": ["🤖 Hermes"],
    }
    fitted = pk.fit_chat(a_packet(**blocks))
    got = dict(fitted["m"])
    assert got["unchecked"][0] == "## Not checked"
    assert 1 < len(got["unchecked"]) < 15
    assert got["attention"] == blocks["attention"]


def test_a_card_left_with_its_title_alone_goes():
    blocks = {
        "attention": ["## Needs attention", *lines_of(20, size=110, tag="a")],
        "unchecked": ["## Not checked", *lines_of(20, size=110, tag="u")],
    }
    fitted = pk.fit_chat(a_packet(**blocks))
    got = dict(fitted["m"])
    assert "unchecked" not in got  # its points went before any of the attention's
    assert got["attention"][0] == "## Needs attention" and len(got["attention"]) > 1
    assert all(any(not line.startswith("## ") for line in lines) for lines in got.values())
    assert pk.size(fitted) <= LIMIT


def test_the_palette_and_the_limit_cards_go_before_what_needs_attention():
    """Review 13.11: a critical screen keeps its reason; the chat's own colours and fewer limit
    cards are the cheaper loss."""
    packet = pk.chat_packet(1, AT, a_view(blocks=()), theme="neon")
    attention = ["## Needs attention", *lines_of(4, size=60, tag="a")]
    packet["m"] = [["attention", attention]]
    card = ["Provider", [[[["Week", 50, 0]], "resets in 5d"]], "Week · resets in 5d"]
    packet["q"] = [[f"{card[0]} {i}", *card[1:]] for i in range(12)]
    assert pk.size(packet) > LIMIT
    fitted = pk.fit_chat(packet)
    assert dict(fitted["m"])["attention"] == attention
    assert "pl" not in fitted and 0 < len(fitted["q"]) < 12
    assert pk.size(fitted) <= LIMIT


def test_blocks_go_in_the_drop_order_the_least_important_first():
    assert pk.DROP_ORDER == ("work", "unchecked", "hermes", "works")
    assert pk.LAST_ORDER == ("note", "attention")
    blocks = {
        "attention": ["## Needs attention", "keep me"],
        "note": ["a note"],
        "works": lines_of(2, tag="k"),
        "hermes": ["🤖 Hermes"],
        "unchecked": ["## Not checked", *lines_of(3, tag="u")],
        "work": lines_of(8, size=110, tag="w"),
    }
    fitted = pk.fit_chat(a_packet(**blocks))
    got = dict(fitted["m"])
    assert len(got.get("work", [])) < 8
    assert got["unchecked"] == blocks["unchecked"] or len(got["work"]) == 0
    assert got["attention"] == blocks["attention"]
    assert got["hermes"] == blocks["hermes"]


def test_a_longer_line_is_clipped_with_an_ellipsis_when_the_packet_is_cut():
    long = "L" * 400
    packet = a_packet(attention=["## Needs attention", long], works=lines_of(10, size=110))
    fitted = pk.fit_chat(packet)
    kept = dict(fitted["m"])["attention"][1]
    assert kept == "L" * (pk.LINE_LIMIT - 1) + "…"


def test_the_cards_go_only_after_the_droppable_blocks_are_emptied():
    packet = a_packet(works=lines_of(6, size=110))
    packet["q"] = [["P", [[[["a", 1, 0]], ""]], "x" * 900]]
    fitted = pk.fit_chat(packet)
    assert fitted["q"] == []
    assert fitted["m"] == []
    assert pk.size(fitted) <= LIMIT


def test_only_a_clipped_headline_is_left_of_a_screen_beyond_any_packet():
    packet = a_packet(works=["w"])
    packet["h"] = "H" * 3000
    packet["pl"] = pk.palette("neon")
    fitted = pk.fit_chat(packet)
    assert fitted["h"] == "H" * (pk.HEADLINE_LIMIT - 1) + "…"
    assert (fitted["m"], fitted["q"]) == ([], [])
    assert "pl" not in fitted
    assert pk.size(fitted) <= LIMIT


def test_encode_raises_past_the_limit_and_returns_the_text_within_it():
    assert pk.encode({"a": "x" * 10}) == '{"a":"xxxxxxxxxx"}'
    with pytest.raises(ValueError, match="1024"):
        pk.encode({"a": "x" * LIMIT})


def test_size_counts_utf8_bytes_not_characters():
    assert pk.size({"a": "ж"}) == len('{"a":"ж"}'.encode())
    with pytest.raises(ValueError):
        pk.encode({"a": "ж" * 600})


def test_a_lone_surrogate_becomes_a_question_mark():
    text = pk.text_of({"a": "x\ud83dy"})
    assert text == '{"a":"x?y"}'
    assert pk.encode({"a": "x\ud83dy"}) == '{"a":"x?y"}'


# ----------------------------------------------------------------------------- cron packet


def test_the_cron_packet_always_carries_the_interface_words():
    packet = pk.cron_packet(a_view(), AT)
    assert packet["x"] == list(pk.UI_WORDS)
    assert packet["h"] == "No data"
    assert (packet["v"], packet["t"]) == (5, AT)


def test_group_headers_have_the_label_the_count_and_the_soonest_run():
    screen = cron_view(
        ("Every 15 min", rows(("ping", 5, "21:05"), ("pong", 20, "21:20"))),
        ("Daily", rows(("digest", 300, "02:00"))),
    )
    packet = pk.cron_packet(screen, AT)
    assert [g[:3] for g in packet["g"]] == [["Every 15 min", 2, "21:05"], ["Daily", 1, "02:00"]]


def test_paused_and_done_are_counts_and_failing_rows_are_copied():
    screen = cron_view(
        paused=(["a", "Sep 28"], ["b", "Sep 29"]),
        done=(["c", "Sep 25"],),
        failing=(["daily-b", "failed", "note"],),
    )
    packet = pk.cron_packet(screen, AT)
    assert (packet["p"], packet["d"]) == (2, 1)
    assert packet["a"] == [["daily-b", "failed", "note"]]
    assert "g" not in packet


def test_without_paused_done_or_failing_those_keys_are_absent():
    packet = pk.cron_packet(cron_view(("Daily", rows(("a", 5, "21:05")))), AT)
    assert not {"p", "d", "a"} & set(packet)


def test_the_nearest_groups_open_by_their_soonest_run_and_the_far_one_stays_folded():
    screen = cron_view(
        ("Daily", rows(("late-1", 300, "02:00"), ("late-2", 301, "02:01"), ("late-3", 302, "x"))),
        ("Every 15 min", rows(("soon-1", 5, "21:05"), ("soon-2", 6, "21:06"))),
        ("Once", rows(*[(f"mid-{i}", 100 + i, "22:40") for i in range(4)])),
    )
    packet = pk.cron_packet(screen, AT)
    assert opened(packet) == ["Every 15 min", "Once"]  # the packet keeps the groups' own order
    got = {g[0]: g[3] for g in packet["g"] if len(g) == 4}
    assert got["Every 15 min"] == [["soon-1", "21:05"], ["soon-2", "21:06"]]
    assert len(got["Once"]) == 4


def test_opening_stops_at_the_first_group_that_does_not_fit_the_row_budget():
    screen = cron_view(
        ("A", rows(*[(f"a{i}", 5 + i, "x") for i in range(3)])),
        ("B", rows(*[(f"b{i}", 20 + i, "x") for i in range(4)])),
        ("C", rows(*[(f"c{i}", 40 + i, "x") for i in range(3)])),
        ("D", rows(("d0", 41, "x"))),
    )
    packet = pk.cron_packet(screen, AT)
    assert pk.CRON_ROWS == 8
    assert opened(packet) == ["A", "B"]  # C needs 3 of the 1 left: D does not jump the queue


def test_two_groups_with_the_same_soonest_run_open_the_smaller_first():
    screen = cron_view(
        ("Big", rows(*[(f"b{i}", 10, "x") for i in range(6)])),
        ("Small", rows(*[(f"s{i}", 10, "x") for i in range(3)])),
    )
    packet = pk.cron_packet(screen, AT)
    assert opened(packet) == ["Small"]


def test_a_group_with_an_unknown_next_run_is_opened_last():
    screen = cron_view(
        ("Other", rows(*[(f"odd-{i}", None, "—") for i in range(7)])),
        ("Daily", rows(("a", 500, "x"), ("b", 501, "x"), ("c", 502, "x"))),
    )
    assert opened(pk.cron_packet(screen, AT)) == ["Daily"]  # Other needs 7 of the 5 left
    assert pk.nearest(screen.cron.groups[0][1]) == math.inf


def test_the_cron_row_budget_is_honoured_for_a_single_big_group():
    screen = cron_view(("Daily", rows(*[(f"job-{i}", i, "x") for i in range(pk.CRON_ROWS + 1)])))
    packet = pk.cron_packet(screen, AT)
    assert opened(packet) == []
    assert packet["g"][0][:2] == ["Daily", pk.CRON_ROWS + 1]


def test_opening_stops_when_the_next_group_would_pass_1024_bytes():
    names = [f"{'n' * 90}{i}" for i in range(8)]
    screen = cron_view(
        *[(f"G{i}", rows((names[i], i, "21:00"))) for i in range(8)],
    )
    packet = pk.cron_packet(screen, AT)
    assert pk.size(packet) <= LIMIT
    count = len(opened(packet))
    assert 0 < count < 8
    assert opened(packet) == [f"G{i}" for i in range(count)]
    assert "cut" not in packet


def test_a_long_failing_list_loses_its_notes_first():
    failing = tuple([f"job-{i}", "failed", "n" * 60] for i in range(14))
    screen = cron_view(("Daily", rows(("a", 5, "21:05"))), failing=failing)
    packet = pk.cron_packet(screen, AT)
    assert pk.size(packet) <= LIMIT
    assert len(packet["a"]) == 14
    assert any(row[2] == "" for row in packet["a"])
    assert "g" in packet


def test_a_long_failing_list_drops_groups_with_a_cut_before_its_own_jobs():
    failing = tuple([f"job-{i}", "failed", ""] for i in range(25))
    groups = [(f"Group {i}", rows((f"j{i}", i, "21:05"))) for i in range(10)]
    packet = pk.cron_packet(cron_view(*groups, failing=failing), AT)
    assert pk.size(packet) <= LIMIT
    assert packet["cut"] == 1
    assert len(packet["a"]) == 25
    assert len(packet["g"]) < 10


def test_failing_jobs_dropped_from_the_end_are_marked_with_a_cut():
    failing = tuple([f"job-{i:02d}", "failed", ""] for i in range(60))
    packet = pk.cron_packet(cron_view(failing=failing), AT)
    assert len(packet["a"]) < 60
    assert packet["cut"] == 1


def test_at_worst_only_the_summary_is_left_and_it_still_carries_the_words():
    screen = cron_view(("Daily", rows(("a", 5, "21:05"))))
    screen = a_view(cron=wv.CronView("H" * 3000, groups=screen.cron.groups))
    packet = pk.cron_packet(screen, AT)
    assert set(packet) == {"v", "t", "h", "x", "cut"}
    assert packet["x"] == list(pk.UI_WORDS)
    assert packet["h"] == "H" * (pk.HEADLINE_LIMIT - 1) + "…"
    assert pk.size(packet) <= LIMIT


# ----------------------------------------------------------------------------- cron answer


def test_a_groups_answer_is_its_rows_of_name_and_when():
    screen = cron_view(("Daily", rows(("a", 5, "21:05"), ("b", None, "—"))))
    assert pk.cron_answer(screen, "Daily") == {"v": 5, "r": [["a", "21:05"], ["b", "—"]]}


def test_paused_and_done_answers_are_name_and_day():
    screen = cron_view(paused=(["a", "Sep 28"],), done=(["b", "Sep 25"],))
    assert pk.cron_answer(screen, "paused") == {"v": 5, "r": [["a", "Sep 28"]]}
    assert pk.cron_answer(screen, "done") == {"v": 5, "r": [["b", "Sep 25"]]}


def test_an_unknown_group_is_gone():
    screen = cron_view(("Daily", rows(("a", 5, "21:05"))))
    assert pk.cron_answer(screen, "Weekly") == {"e": "gone"}
    assert pk.cron_answer(a_view(), "Daily") == {"e": "gone"}


def test_an_answer_over_the_limit_shortens_the_names_down_to_the_name_limit_first():
    screen = cron_view(("Daily", rows(*[(f"{i:02d}" + "Ж" * 38, i, "21:05") for i in range(12)])))
    answer = pk.cron_answer(screen, "Daily")
    assert pk.size(answer) <= LIMIT
    assert "cut" not in answer
    assert len(answer["r"]) == 12
    lengths = {len(row[0]) for row in answer["r"]}
    assert max(lengths) < 40
    assert min(lengths) >= pk.CRON_NAME_LIMIT


def test_an_answer_still_over_at_the_name_limit_drops_rows_from_the_end_with_a_cut():
    screen = cron_view(("Daily", rows(*[(f"{i:02d}" + "Ж" * 38, i, "21:05") for i in range(90)])))
    answer = pk.cron_answer(screen, "Daily")
    assert answer["cut"] == 1
    assert pk.size(answer) <= LIMIT
    assert 0 < len(answer["r"]) < 90
    assert all(len(row[0]) == pk.CRON_NAME_LIMIT for row in answer["r"])
    assert answer["r"][0][0].startswith("00")  # the soonest jobs stay


# ----------------------------------------------------------------------------- details


def test_the_details_packet_titles_the_groups_in_the_dashboards_words():
    screen = a_view(details=(("backup", ("ok",)), ("no data", ("x",)), ("odd", ("y",))))
    packet = pk.details_packet(screen)
    assert packet == {"v": 5, "d": [["Backup & drift", ["ok"]], ["No data", ["x"]], ["Odd", ["y"]]]}


def test_an_oversize_details_packet_is_cut_and_keeps_the_start_of_every_group_it_can():
    big = tuple(("backup" if i == 0 else "memory", tuple(lines_of(10, size=110))) for i in range(2))
    packet = pk.details_packet(a_view(details=big))
    assert packet["cut"] == 1
    assert pk.size(packet) <= LIMIT
    assert all(len(line) <= pk.LINE_LIMIT for _, lines in packet["d"] for line in lines)


# ----------------------------------------------------------------------------- palettes


def luminance(hex6: str) -> float:
    def channel(i: int) -> float:
        c = int(hex6[i : i + 2], 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4)


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def oklch(hex6: str) -> tuple[float, float, float]:
    def channel(i: int) -> float:
        c = int(hex6[i : i + 2], 16) / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(0), channel(2), channel(4)
    lms = [
        x ** (1 / 3)
        for x in (
            0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
            0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
            0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
        )
    ]
    light = 0.2104542553 * lms[0] + 0.7936177850 * lms[1] - 0.0040720468 * lms[2]
    a = 1.9779984951 * lms[0] - 2.4285922050 * lms[1] + 0.4505937099 * lms[2]
    bb = 0.0259040371 * lms[0] + 0.7827717662 * lms[1] - 0.8086757660 * lms[2]
    return light, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def hue_gap(a: str, b: str) -> float:
    gap = abs(oklch(a)[2] - oklch(b)[2])
    return min(gap, 360 - gap)


@pytest.mark.parametrize("name", list(pk.PALETTES))
def test_a_palette_lists_every_token_in_the_widgets_order(name):
    text = pk.palette(name)
    colours = pk.colours(name)
    assert set(pk.TOKENS) <= set(colours)
    assert text == ";".join(f"--{token}:#{colours[token]}" for token in pk.TOKENS)
    assert text.count("--") == len(pk.TOKENS)
    assert all(len(colours[token]) == 6 for token in pk.TOKENS)


def test_the_chats_own_colours_and_an_unknown_name_have_no_palette():
    assert pk.palette("telegram") is None
    assert pk.palette("pink") is None
    assert pk.colours("pink") is None


def test_mix_blends_channel_by_channel():
    assert pk.mix("ffffff", "000000", 0.5) == "808080"
    assert pk.mix("ff0000", "0000ff", 1.0) == "ff0000"


@pytest.mark.parametrize("name", list(pk.PALETTES))
def test_palette_text_keeps_its_contrast(name):
    c = pk.colours(name)
    pairs = (
        ("ink", "card"),
        ("mut", "card"),
        ("mut", "bg"),
        ("ink", "alw"),
        ("ink", "alc"),
        ("mut", "alc"),
        ("on", "acc"),
        ("acc", "card"),
        ("val", "card"),
    )
    for fg, bg in pairs:
        assert contrast(c[fg], c[bg]) >= 4.5, (name, fg, bg)


@pytest.mark.parametrize("name", list(pk.PALETTES))
def test_bars_warn_and_crit_keep_three_to_one_against_the_tracks(name):
    c = pk.colours(name)
    for mark in ("bar", "bar2", "warn", "crit"):
        assert contrast(c[mark], c["trk"]) >= 3.0, (name, mark)


@pytest.mark.parametrize("name", list(pk.PALETTES))
def test_warn_and_crit_differ_from_the_accent_and_the_bars_in_hue(name):
    c = pk.colours(name)
    for signal in ("warn", "crit"):
        for ref in ("acc", "bar", "bar2"):
            if oklch(c[ref])[1] < 0.03:
                continue  # an accent without a hue (mono) differs from any signal colour
            assert hue_gap(c[signal], c[ref]) >= 35, (name, signal, ref)
    assert hue_gap(c["warn"], c["crit"]) >= 35


def test_the_ember_attention_card_is_warm_amber_not_olive():
    ember = pk.PALETTES["ember"]
    assert oklch(pk.colours("ember")["alw"])[2] < 80
    assert oklch(pk.mix(ember["warn"], ember["card"], 0.2))[2] > 90


def test_tidy_drops_a_block_with_no_lines_or_its_title_alone():
    packet = {
        "m": [
            ["unchecked", ["## Not checked"]],
            ["works", []],
            ["attention", ["## Needs attention", "Max: rules outdated"]],
            ["hermes", ["🤖 Hermes 0.23.1"]],
        ]
    }
    pk._tidy(packet)
    assert [key for key, _ in packet["m"]] == ["attention", "hermes"]
