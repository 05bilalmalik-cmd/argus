"""Retain an owned leading hint without borrowing a previous control's help."""
import pytest

from app.automation.field_context import extract_field_context

HINT = "Enter your institution as shown on your transcript"


@pytest.mark.parametrize("container", ["form", "fieldset", "div"])
@pytest.mark.parametrize("placement", ["before_label", "after_label", "after_control"])
def test_single_control_preserves_owned_hint(container, placement):
    label = '<label for="x">University</label>'
    control = '<input id="x">'
    hint = f'<small class="hint">{HINT}</small>'
    parts = {
        "before_label": hint + label + control,
        "after_label": label + hint + control,
        "after_control": label + control + hint,
    }
    html = f'<{container}>{parts[placement]}</{container}>'
    context = extract_field_context(html, html, selector="#x")
    assert context.label == "University"
    assert context.help_text == HINT


@pytest.mark.parametrize("preceding", [
    '<label for="a">Given name</label><input id="a">',
    '<div><label for="a">Given name</label><input id="a"></div>',
])
def test_previous_control_hint_is_not_owned_by_next_control(preceding):
    html = f'<form>{preceding}<small class="hint">Foreign legal name hint</small><label for="x">University</label><input id="x"></form>'
    context = extract_field_context(html, html, selector="#x")
    assert context.label == "University"
    assert context.help_text == ""


def test_first_control_keeps_leading_hint_when_later_control_exists():
    html = f'<form><small class="hint">{HINT}</small><label for="x">University</label><input id="x"><label for="y">Degree</label><input id="y"></form>'
    assert extract_field_context(html, html, selector="#x").help_text == HINT
