# FieldContext — structural HTML context extractor

## Why

`DeterministicClassifier` (`app/automation/classifier.py`) matches mostly on a
question's own label/name/placeholder text. Custom career portals (Rothschild,
Point72, Jane Street, Moelis, Carlyle, Perella Weinberg, Wells Fargo, PSP,
Neuberger Berman) use nonstandard `name` attributes, so those fields map to
`UNKNOWN` and the application stalls. `FieldContext`
(`app/automation/field_context.py`) extracts everything *around* the field that
hints at its meaning; a separate consumer agent feeds it to the classifier.

## API

```python
from app.automation.field_context import (
    FieldContext, extract_field_context, field_context_to_question,
)

ctx = extract_field_context(field_html, document_html, selector="input[name='q_spons']")
question = ctx.to_form_question()  # -> FormQuestion, enriched label + options
```

- `field_html`: snippet of the `<input>`/`<select>`/`<textarea>`.
- `document_html`: full page; used for `for=`/`id` labels, `aria-*` targets,
  fieldset legends, preceding headings, and radio/checkbox group members.
- `selector` (optional): CSS selector locating the field in the document.
  Without it, the snippet is aligned to its document twin by `id`, else by
  tag/type/`name`.
- Never raises: malformed HTML, missing labels, or nameless fields yield an
  empty/partial `FieldContext`.

## Extracted signals

| Field | Source |
|---|---|
| `label` | `label[for=id]` → wrapping `<label>` → `aria-label` → `aria-labelledby` |
| `placeholder` / `title` / `name` / `element_id` / `autocomplete` | element attributes |
| `fieldset_legend` | enclosing `<fieldset>`'s `<legend>` |
| `help_text` | `aria-describedby` targets + adjacent hint siblings (`small`, `*-hint/help/desc*` classes) |
| `section_heading` | nearest preceding `h1`–`h6` / `[role=heading]` in document order ("Education", "Work Authorisation", "Diversity") |
| `group_label` / `group_options` | radio/checkbox same-`name` members: shared legend/question + every option label (`for=` → wrapping → `aria-label` → `value`) |
| `required` / `required_evidence` | `required` attr, `aria-required="true"`, `*` in label |
| `input_type` | normalised tag/`type` (`select`, `textarea`, `radio`, …) |
| `select_options` / `option_values` | `<option>` visible texts / `value` attributes |
| `context_text` | normalised combination in priority order: label → group question → legend → heading → placeholder/title/help → options → name/id/autocomplete tokens |

## Bridge to the existing model

No parallel model: `FieldContext.to_form_question()` (alias
`field_context_to_question`) returns a `FormQuestion` whose `label` joins
label/group/legend/heading and whose `options` carry group or select options —
directly consumable by `DeterministicClassifier`. Proven in test:
bare `FormQuestion(label="", field_type="radio", name="q_spons")` → `UNKNOWN`,
enriched context → `SPONSORSHIP`.

## Dependencies / constraints

- `beautifulsoup4` only (already in `pyproject.toml`); deterministic, no ML.
- New files only: `app/automation/field_context.py`,
  `tests/unit/test_field_context.py`, this report. Nothing else touched.

## Verification

- `pytest tests/unit/test_field_context.py -q` → **14 passed**.
- `pytest tests/unit -q -k "classifier or question or generic"` → **96 passed**.
