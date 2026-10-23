"""Подготовка markdown к рендеру: формулы, которые pandoc разорвал бы абзацем."""

from absorbobot.workers.render import _collapse_display_math


def test_blank_line_inside_display_math_is_collapsed():
    text = ("Правило цепи даёт\n"
            "\\[\n"
            "\\frac{\\partial L}{\\partial E_{ij}}\n"
            "=\n"
            "\n"
            "+\n"
            "G_{\\text{output}}. \n"
            "\\]\n")
    assert _collapse_display_math(text) == (
        "Правило цепи даёт\n"
        "\\[ \\frac{\\partial L}{\\partial E_{ij}} = + G_{\\text{output}}. \\]\n")


def test_list_marker_inside_display_math_is_collapsed():
    text = "\\[\na\n- b\n\\]\n"
    assert _collapse_display_math(text) == "\\[ a - b \\]\n"


def test_dollar_display_math_with_blank_line_is_collapsed():
    text = "$$ x = 1\n\n+ y $$\n"
    assert _collapse_display_math(text) == "$$ x = 1 + y $$\n"


def test_indent_of_formula_inside_list_item_is_kept():
    text = "  \\[\n  a\n\n  2. b\n  \\]\n"
    assert _collapse_display_math(text) == "  \\[ a 2. b \\]\n"


def test_well_formed_display_math_is_untouched():
    text = "до\n\\[\na = b \\\\\nc = d\n\\]\nпосле\n"
    assert _collapse_display_math(text) == text


def test_single_line_math_is_untouched():
    text = "значение \\[a=b\\] в тексте\n"
    assert _collapse_display_math(text) == text


def test_fenced_code_block_is_untouched():
    text = "```latex\n\\[\na\n\nb\n\\]\n```\n"
    assert _collapse_display_math(text) == text


def test_unterminated_display_math_is_untouched():
    text = "текст\n\\[\na = b\n"
    assert _collapse_display_math(text) == text


def test_collapse_is_idempotent():
    text = "\\[\na\n\n+ b\n\\]\n"
    once = _collapse_display_math(text)
    assert _collapse_display_math(once) == once
