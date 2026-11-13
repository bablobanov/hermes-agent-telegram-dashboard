"""``widget/widget.html`` as it is sent (``htmlmin``): its size, its words and its constructs."""

import re
import zlib

import pytest

from telegram_dashboard import widget
from telegram_dashboard.widget import htmlmin, packets

# What Telegram allows: 4096 bytes of the HTML after zlib level 9. The margin below it is the
# owner's decision of 13.11: the beta margin was ~70 (measured 62 on zlib 1.3 and 54 on zlib-ng);
# Telegram counts zlib-9 of the sent HTML, and the build of zlib differs from machine to machine.
HTML_LIMIT = 4096
HTML_MARGIN = 50

SENT = widget.html()
SOURCE = widget.HTML_PATH.read_text(encoding="utf-8")


def script_of(html: str) -> str:
    found = re.search(r"<script>(.*?)</script>", html, re.S)
    assert found is not None
    return found.group(1)


# ----------------------------------------------------------------------------- the sent HTML


def test_the_sent_html_compressed_at_level_nine_keeps_the_margin_under_4096_bytes():
    packed = len(zlib.compress(SENT.encode("utf-8"), 9))
    assert packed <= HTML_LIMIT - HTML_MARGIN, f"{packed} bytes after zlib-9 (margin {HTML_MARGIN})"


def test_the_sent_html_is_the_minified_source():
    assert htmlmin.minify(SOURCE) == SENT
    assert len(SENT) < len(SOURCE)
    assert htmlmin.zlib_size(SENT) == len(zlib.compress(SENT.encode("utf-8"), 9))


def test_the_sent_html_has_no_comments_and_no_indentation():
    assert "/*" not in SENT
    assert not re.search(r"(?m)^\s*//", SENT)
    assert "\n  " not in SENT


def test_the_sent_html_is_a_style_a_body_and_one_script():
    assert SENT.startswith("<style>")
    assert SENT.count("<script>") == SENT.count("</script>") == 1
    assert SENT.endswith("</script>")


# ----------------------------------------------------------------------------- the words


def test_every_word_index_the_html_reads_is_one_the_packet_carries():
    inside = re.findall(r"\bX\[([^\]]*)\]", SENT)
    used = {int(n) for text in inside for n in re.findall(r"\d+", text)}
    assert used, "the HTML reads no interface words"
    assert max(used) < len(packets.UI_WORDS)


def test_every_interface_word_is_read_somewhere_in_the_html():
    inside = re.findall(r"\bX\[([^\]]*)\]", SENT)
    reads = {n for text in inside for n in re.findall(r"\d+", text)}
    assert {str(i) for i in range(len(packets.UI_WORDS))} - reads <= {"14", "15"}  # X[13 + i]


def test_the_words_are_in_the_order_the_html_reads_them():
    words = packets.UI_WORDS
    assert words[0] == "Refresh"
    assert words[3] == "Loading…"
    assert words[13:16] == ("Status", "Cron", "Details")
    assert words[16] == "Theme: "
    assert len(words) == 17


@pytest.mark.parametrize(
    ("snippet", "index"),
    [
        (r"T\(\$\('go'\),X\[0\]\)", 0),
        (r"T\(b,X\[1\]\)", 1),
        (r"div\('c12',X\[4\]\)", 4),
        (r"X\[13\+i\]", 13),
        (r"'<button id=\"th\">'\+X\[16\]", 16),
        (r"X\[12\]", 12),
        (r"X\[8\]", 8),
        (r"X\[9\]\+P\.a\.length", 9),
    ],
)
def test_the_html_reads_each_word_where_it_means_it(snippet, index):
    assert re.search(snippet, SENT), f"X[{index}] not read as expected"


def test_the_interface_words_travel_in_the_cron_packet_not_in_the_html():
    for word in ("Refresh", "Loading…", "Paused", "Theme: "):
        assert word not in SENT
    assert packets.UI_WORDS[0] == "Refresh"


# ----------------------------------------------------------------------------- constructs


def test_no_resize_listener_and_no_local_storage():
    assert "addEventListener('resize'" not in SENT
    assert 'addEventListener("resize"' not in SENT
    assert "localStorage" not in SENT
    assert "sessionStorage" not in SENT


def test_tabs_are_hidden_with_style_display_not_the_hidden_attribute():
    script = script_of(SENT)
    assert "style.display" in script
    assert not re.search(r"\.hidden\s*=", script)
    assert "'hidden'" not in script.replace("visibility='hidden'", "")
    assert not re.search(r"<[a-z][^>]*\shidden[\s>=]", SENT)


def test_group_bodies_animate_max_height():
    assert re.search(r"\.gb\{[^}]*transition:max-height", SENT)
    assert re.search(r"\.gb\{[^}]*max-height:0", SENT)
    assert re.search(r"\.o>\.gb\{max-height:none\}", SENT)


def test_the_theme_button_asks_for_the_next_theme_the_bot_named():
    assert 'id="th"' in SENT
    assert re.search(r"'theme:'\s*\+\s*C\.nx", SENT)
    assert re.search(r"b\.id\s*==\s*'th'", SENT)


def test_the_escaper_covers_the_double_quote_written_as_a_hex_escape():
    escaper = re.search(r"E=\(?s\)?=>String\(s\)\.replace\(/(\[[^\]]*\])/g", SENT)
    assert escaper is not None, "the E() escaper is not found"
    assert "\\x22" in escaper.group(1)
    assert '"' not in escaper.group(1)


def test_no_regex_literal_holds_a_raw_double_quote():
    """htmlmin reads no regex literals: a quote in one would open a string and swallow the rest."""
    script = script_of(SENT)
    assert not re.search(r"/\[[^\]/\n]*\"[^\]/\n]*\]", script)
    assert not re.search(r"/\[[^\]/\n]*'[^\]/\n]*\]/", script)


def test_the_source_keeps_its_regex_literals_free_of_quotes_too():
    for found in re.finditer(r"\.replace\(/([^/\n]+)/", SOURCE):
        assert '"' not in found.group(1), found.group(0)


def test_the_script_has_balanced_brackets_outside_its_strings():
    code = re.sub(htmlmin.STRING, "''", script_of(SENT))
    code = re.sub(r"/\[[^\]\n]*\]/[gimsuy]*", "0", code)
    for opening, closing in ("()", "[]", "{}"):
        assert code.count(opening) == code.count(closing), opening


# ----------------------------------------------------------------------------- htmlmin


def test_string_literals_are_kept_verbatim_in_both_quotes():
    out = htmlmin.script("const a  =  'it   is  (x)=>  ;}';\nconst b = \"q   ;} const  z\";")
    assert "'it   is  (x)=>  ;}'" in out
    assert '"q   ;} const  z"' in out


def test_an_escaped_quote_does_not_end_a_string():
    out = htmlmin.script("const a = 'don\\'t   stop';  const b = 1;")
    assert "'don\\'t   stop'" in out
    assert out.endswith("let b=1;")


def test_an_arrow_with_one_parameter_loses_its_parentheses():
    assert htmlmin.script("const f = (x) => x + 1;") == "let f=x=>x+1;"


def test_an_arrow_with_two_parameters_or_none_keeps_them():
    out = htmlmin.script("const f = (a, b) => a + b, g = () => 1;")
    assert "(a,b)=>" in out
    assert "()=>" in out


def test_const_becomes_let_but_not_inside_a_word():
    assert htmlmin.script("const a = 1; const b = 2;") == "let a=1;let b=2;"
    assert "myconst x" in htmlmin.script("myconst x = 1;")


def test_a_semicolon_before_a_closing_brace_goes_outside_strings_only():
    out = htmlmin.script("if (a) { b(); c('x;}'); }")
    assert out == "if(a){b();c('x;}')}"


def test_whole_line_slash_comments_go():
    out = htmlmin.script("// one\nlet a = 1;\n   // two\nlet b = 2;\n")
    assert out == "let a=1;let b=2;"


def test_a_slash_comment_sign_inside_a_string_is_not_a_comment():
    assert htmlmin.script("let u = 'http://x';") == "let u='http://x';"


def test_spaces_between_two_word_characters_stay_and_punctuation_loses_its():
    assert htmlmin.script("return  x  +  y ;") == "return x+y;"
    assert htmlmin.script("a = b ? c : d;") == "a=b?c:d;"


def test_a_plus_before_a_plus_keeps_its_space():
    assert htmlmin.script("a + +b; c - -d;") == "a+ +b;c- -d;"


def test_a_line_break_stays_where_a_call_ends_and_a_parenthesis_begins():
    assert htmlmin.script("f()\n(g)") == "f()\n(g)"
    assert htmlmin.script("a = 'b'\n[1]") == "a='b'\n[1]"


def test_a_line_break_stays_where_a_brace_meets_a_word():
    out = htmlmin.script("function f() {\n  return 1;\n}\nlet x = 2;")
    assert out == "function f(){return 1}\nlet x=2;"


def test_css_comments_and_spaces_around_the_punctuation_go():
    css = "/* c */\n.a , .b > .c {\n  color : red ;\n  margin : 0 4px ;\n}\n/* end */"
    assert htmlmin.style(css) == ".a,.b>.c{color:red;margin:0 4px}"


def test_a_css_value_keeps_its_inner_spaces():
    assert htmlmin.style("a{transition : max-height .2s ;}") == "a{transition:max-height .2s}"


def test_minify_handles_the_style_and_the_script_and_joins_the_tags():
    html = "<style>\n/* c */ a { b : c ; }\n</style>\n<div>\n</div>\n<script>\n// x\nconst a = 1;\n</script>\n"
    assert htmlmin.minify(html) == "<style>a{b:c}</style><div></div><script>let a=1;</script>"


def test_minify_leaves_the_markup_text_between_tags_alone():
    assert htmlmin.minify("<p>a  b</p>") == "<p>a  b</p>"


def test_the_words_of_the_cron_packet_redraw_the_status_and_label_the_tabs_first():
    """Review 13.11: the interface's words come with publicData; the Status tab and the tabs are
    labelled again when it changes, and the tabs before a missing chatData returns."""
    source = SOURCE
    assert "X = P?.x || X; cron(); draw();" in source
    body = source[source.index("function draw() {") :]
    assert body.index("tabs.forEach((b, i) => T(b, X[13 + i]))") < body.index("if (!C) return")


def test_the_theme_answer_is_taken_like_a_tick():
    """The answer is the whole screen in the theme, numbered: taken as a packet, so a palette it
    leaves out (the chat's own colours) is gone from the screen too (bench, 13.11)."""
    assert "ask(b, 'theme:' + C.nx, (r) => (take(r), draw()))" in SOURCE
    assert "Object.assign(C" not in SOURCE
