"""The widget's HTML as it is sent: comments, indentation and spaces around punctuation out.

Telegram takes at most 4096 bytes of the HTML after zlib at level 9 (``sendWidget``); the
readable source in ``widget.html`` is over that, the sent text is under it. Only what the widget's
own HTML uses is handled, no general minifier: in the script a whole-line ``//`` comment goes,
string literals stay as they are, a space stays between two word characters and in ``+ +`` /
``- -``, a line break stays where a line ending in ``)``, ``]``, ``}`` or a quote meets one
starting with ``(``, ``[`` or a backtick, and where ``}`` meets a word (no automatic semicolon is
lost); in the stylesheet comments, line breaks and the spaces around ``{};:,>`` go.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable

WORD = re.compile(r"[A-Za-z0-9_$]")
STRING = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


def _keep(code: str, start: int, end: int) -> str:
    """What a run of whitespace between ``code[start - 1]`` and ``code[end]`` becomes."""
    before, after = code[start - 1 : start], code[end : end + 1]
    broken = "\n" in code[start:end]
    if WORD.match(before or " ") and WORD.match(after or " "):
        return "\n" if broken else " "
    if before + after in ("++", "--"):
        return " "
    if (
        broken
        and before
        and after
        and ((before in ")]}'\"" and after in "([`") or (before == "}" and WORD.match(after)))
    ):
        return "\n"
    return ""


def script(code: str) -> str:
    """Whitespace outside string literals, judged by its neighbours in the whole script."""
    lines = [line.strip() for line in code.split("\n")]
    code = "\n".join(line for line in lines if line and not line.startswith("//"))
    strings = [match.span() for match in STRING.finditer(code)]
    out, last, index = [], 0, 0
    for match in re.finditer(r"\s+", code):
        while index < len(strings) and strings[index][1] <= match.start():
            index += 1
        if index < len(strings) and strings[index][0] <= match.start():
            continue  # inside a string literal
        out += [code[last : match.start()], _keep(code, match.start(), match.end())]
        last = match.end()
    out.append(code[last:])
    return _outside_strings("".join(out), _shorter)


def _shorter(code: str) -> str:
    """Shorter spellings of the same code: ``(x)=>`` is ``x=>``, ``const`` is ``let`` (nothing
    is assigned twice by mistake once the source is tested), no ``;`` before ``}``."""
    code = re.sub(r"\((\w+)\)=>", r"\1=>", code)
    code = re.sub(r"\bconst ", "let ", code)
    return code.replace(";}", "}")


def _outside_strings(code: str, change: Callable[[str], str]) -> str:
    """``change`` applied to the code between string literals (the source keeps quotes out of its
    regular expressions: ``\\x22``)."""
    out, last = [], 0
    for match in STRING.finditer(code):
        out += [change(code[last : match.start()]), match.group()]
        last = match.end()
    out.append(change(code[last:]))
    return "".join(out)


def style(code: str) -> str:
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"\s*\n\s*", "", code)
    code = re.sub(r"\s+", " ", code)
    code = re.sub(r"\s*([{};:,>])\s*", r"\1", code)
    return code.replace(";}", "}")


def minify(html: str) -> str:
    html = re.sub(
        r"<style>(.*?)</style>",
        lambda m: "<style>" + style(m.group(1)) + "</style>",
        html,
        flags=re.S,
    )
    html = re.sub(
        r"<script>(.*?)</script>",
        lambda m: "<script>" + script(m.group(1)) + "</script>",
        html,
        flags=re.S,
    )
    return re.sub(r">\s*\n\s*<", "><", html).strip()


def zlib_size(text: str) -> int:
    """The size Telegram counts: Python's ``zlib.compress`` at level 9."""
    return len(zlib.compress(text.encode("utf-8"), 9))
