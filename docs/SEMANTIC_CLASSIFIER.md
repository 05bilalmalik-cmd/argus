# Semantic field classifier (non-ATS portals)

## Approach

Custom career portals (Rothschild, Point72, Jane Street, Moelis, Carlyle,
Perella Weinberg, Wells Fargo, PSP, Neuberger Berman) use nonstandard field
naming, so `DeterministicClassifier` (pure regex/substring) returns `UNKNOWN`
and applications stall on `unknown_required_field`.

New second-chance layer in `app/automation/semantic_classifier.py`:

- **Deterministic stays first and authoritative.** `SemanticClassifier` is
  consulted only when the deterministic result is `UNKNOWN` with confidence
  `0.0` ("No deterministic mapping"). Confident mappings are never
  overridden -- including the deliberate `UNKNOWN` escalations (compensation,
  internship history, ambiguous school/start/end dates), which return earlier
  with confidence 0.4/0.5 and never reach the semantic layer.
- **Mapping only, never content.** It returns which `CanonicalKey` a field
  is, with a confidence score and human-readable reason (`QuestionMapping`
  shape). "Postcode*" -> `POSTCODE` is in scope; cover letters and
  free-text answers stay `UNKNOWN`/escalated.
- **Stdlib only, offline.** Nearest-neighbour over a curated paraphrase
  corpus per key (e.g. POSTCODE: "post code", "zip", "postal code"),
  scored as `0.6 * token-Jaccard + 0.4 * difflib.SequenceMatcher`, best
  phrase per key, best key overall. No sklearn/torch/transformers, no model
  download. Below the configurable threshold (default `0.45`, via
  `ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD` or constructor arg) it returns
  `UNKNOWN`.
- **Richer-context hook.** `classify(question, extra_context="")` accepts an
  optional plain string so a future FieldContext provider can feed text in.
  The module deliberately does NOT import that provider.
- **Chaining** (`app/automation/classifier.py`, minimal edit): one import,
  plus the tail of `DeterministicClassifier.classify` consults
  `SemanticClassifier` when the flag is on. Behaviour with the flag off is
  byte-identical (guarded by `test_flag_off_is_byte_identical_to_deterministic`).

## Hard safety rule (refusal)

If the best guess lands in `REFUSED_KEYS` -- `CRIMINAL_RECORD`,
`SPONSORSHIP`, `WORK_AUTHORISATION`, `LEGAL_ATTESTATION`, `DEMOGRAPHIC`,
`ASSESSMENT`, `CAPTCHA` -- the layer returns `UNKNOWN` (confidence 0.0) with
an escalation reason, checked BEFORE the threshold so a sensitive guess can
never slip through. Refused-key phrases exist in the corpus precisely so
obvious matches resolve to the refused key internally and are then refused.
Result: `test_sensitive_keys_are_always_refused` passes 8/8 probes (7 keys,
DEMOGRAPHIC covered twice), plus `test_refusal_holds_through_the_chained_classifier`.

## Flag

- Name: `ARGUS_SEMANTIC_CLASSIFIER_ENABLED`
- Default: **FALSE** (off). Parsed with the exact `_parse_bool` pattern from
  `app/config.py` (copied locally; `app/config.py` untouched -- another agent
  owns it).
- **Follow-up for the config owner:** move this flag (and optionally
  `ARGUS_SEMANTIC_CLASSIFIER_THRESHOLD`) into `Settings` in `app/config.py`.

## Test output (real)

`./.venv/Scripts/python.exe -m pytest tests/unit/test_semantic_classifier.py -q`
-> **34 passed**.

`./.venv/Scripts/python.exe -m pytest tests/unit -q -k "classifier or question or planning or runner"`
-> **154 passed, 1654 deselected** (1 pre-existing Starlette deprecation warning).

## Files

- Created: `app/automation/semantic_classifier.py`,
  `tests/unit/test_semantic_classifier.py`, `docs/SEMANTIC_CLASSIFIER.md`
- Edited (minimal): `app/automation/classifier.py` (import + UNKNOWN tail)
- Untouched per task rules: `app/models.py`, `app/db.py`,
  `app/services/profile.py`, `app/services/navigator.py`,
  `app/automation/field_context.py`, `app/config.py`. No git commands, no
  restarts, no submissions, no DB writes.
