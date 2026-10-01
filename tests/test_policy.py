"""What ``sanitize_public_text`` lets through to a public screen and what it masks."""

import pytest

from telegram_dashboard.policy import sanitize_public_text


# Decision of 01.10 (0.9.0): a chat id and a delivery target are as private as a secret or a
# path. The reasons of the external sources, Grok and Kimi, the incident titles and the backup
# details all pass through here, so the mask is the second line behind "the kind of the error in
# a word" that the cron sources apply first.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Chat not found: -1001234567890", "Chat not found: [id]"),
        ("deliver telegram:-1001234567890:17 failed", "deliver [target] failed"),
        ("target discord:123456789012345678", "target [target]"),
        ("home coder telegram:987654321", "home coder [target]"),
        ("user 987654321 not allowed", "user [id] not allowed"),
        ("bot 123456789:AAE-abcdefghijklmnopqrstuv unauthorized", "bot [secret] unauthorized"),
        (
            "HTTP 429 at 12:05, 481 keys, 120,000 tokens, 0.123456789",
            "HTTP 429 at 12:05, 481 keys, 120,000 tokens, 0.123456789",
        ),
        (
            "commit a409e9a7e0a20009613e4be5f80b4676f9e4cd99 of v2026.9.14",
            "commit a409e9a7e0a20009613e4be5f80b4676f9e4cd99 of v2026.9.14",
        ),
        (
            "2026-09-30T12:05:00+00:00 seen, Data 07:21",
            "2026-09-30T12:05:00+00:00 seen, Data 07:21",
        ),
    ],
    ids=[
        "bare-chat-id",
        "telegram-target-with-topic",
        "discord-target",
        "telegram-target-user",
        "user-id",
        "bot-token-is-a-secret-first",
        "screen-numbers-stay",
        "hash-stays",
        "timestamps-stay",
    ],
)
def test_chat_ids_and_delivery_targets_are_masked_and_the_screen_s_numbers_are_not(
    text: str, expected: str
) -> None:
    assert sanitize_public_text(text) == expected
