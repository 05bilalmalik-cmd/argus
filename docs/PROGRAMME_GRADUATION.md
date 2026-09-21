# Programme Graduation Year Logic & Migration

## Domain Rule

A candidate's graduation year determines eligibility for different programme types:

| Programme Type | Degree Length | Graduation Year | CV Variant |
|----------------|---------------|-----------------|------------|
| **Summer** (3-year degree) | 3 years | 2028 | `summer-cv` |
| **Spring Week / Year in Industry** (4-year degree with placement) | 4 years | 2029 | `yii-cv` |

Both are genuinely true: a 3-year degree exits in 2028 for summer internships; a 4-year degree with placement exits in 2029 for spring week / year-in-industry programmes. The **candidate elects which applies** by declaring their admissible graduation years.

## Migration

### Schema Change
- **Column added**: `admissible_graduation_years_json` (TEXT, NOT NULL DEFAULT `'[]'`) to `candidate_profiles`
- **Schema version**: 9 → 10
- **Migration path**: Registered in `_ADDITIVE_MIGRATIONS` in `app/db.py:30-48`

### How it works
1. On startup, `Database._upgrade_schema()` checks `PRAGMA user_version`
2. If version < 10, runs additive migrations from `_ADDITIVE_MIGRATIONS`
3. For `candidate_profiles`, executes:
   ```sql
   ALTER TABLE "candidate_profiles" ADD COLUMN "admissible_graduation_years_json" TEXT NOT NULL DEFAULT '[]'
   ```
4. Updates `PRAGMA user_version = 10`

### Verification (before/after on live DB copy)

**BEFORE (schema v9):**
```
(0, 'id', 'INTEGER', 1, None, 1)
(1, 'first_name', 'VARCHAR(120)', 1, None, 0)
...
(15, 'preferred_locations_json', 'TEXT', 1, None, 0)
(16, 'work_authorisation_ciphertext', 'TEXT', 1, None, 0)
(17, 'sponsorship_required_ciphertext', 'TEXT', 1, None, 0)
(18, 'work_authorisation_approved', 'BOOLEAN', 1, None, 0)
(19, 'updated_at', 'DATETIME', 1, None, 0)
Schema version: 9
```

**AFTER (schema v10):**
```
(0, 'id', 'INTEGER', 1, None, 1)
...
(15, 'preferred_locations_json', 'TEXT', 1, None, 0)
(16, 'work_authorisation_ciphertext', 'TEXT', 1, None, 0)
(17, 'sponsorship_required_ciphertext', 'TEXT', 1, None, 0)
(18, 'work_authorisation_approved', 'BOOLEAN', 1, None, 0)
(19, 'updated_at', 'DATETIME', 1, None, 0)
(20, 'admissible_graduation_years_json', 'TEXT', 1, "'[]'", 0)
Schema version: 10
Default value: ('[]',)
```

The column is added with the correct default `'[]'` and existing rows inherit it automatically.

## Profile Logic (`app/services/profile.py`)

### `get_automation_data(framing)` behaviour

| Profile admissible years | Framing year | Result |
|--------------------------|--------------|--------|
| `{2028, 2029}` | 2028 (Summer) | **Use framing year (2028), NO conflict** |
| `{2028, 2029}` | 2029 (Spring/Year) | **Use framing year (2029), NO conflict** |
| `{2028, 2029}` | 2030 | **Conflict raised** (framing year not admissible) |
| *not set* (empty) | 2028, stored=2028 | Use stored (2028), no conflict (legacy behaviour) |
| *not set* (empty) | 2028, stored=2029 | **Conflict raised** (legacy behaviour) |
| *any* | `None` | Use stored graduation_year (legacy behaviour) |

### Conflict Guard Keys
When conflict is raised, these guard keys are set for the runner:
- `guard.programme_graduation_conflict = True`
- `guard.programme_graduation_stored = <stored_year>`
- `guard.programme_graduation_tier = <framing_year>`
- `guard.programme_admissible_years = <admissible_tuple>` (only when admissible set declared)

The runner blanks all graduation-derived fields when the conflict guard is present.

## Tests

All tests in `tests/unit/test_programme_graduation.py`:

```
test_admissible_include_summer_framing_no_conflict          PASSED
test_admissible_include_spring_week_framing_no_conflict     PASSED
test_admissible_exclude_framing_conflict_raised             PASSED (MANDATORY negative test)
test_no_admissible_stored_matches_summer_framing_no_conflict PASSED
test_no_admissible_stored_differs_summer_framing_conflict   PASSED (regression guard)
test_framing_none_uses_stored_year                          PASSED
test_existing_db_without_column_gets_it_added               PASSED (migration test)
test_update_admissible_graduation_years                     PASSED
test_summer_classification                                  PASSED
test_spring_week_classification                             PASSED
test_year_in_industry_classification                        PASSED
```

Run with:
```bash
./.venv/Scripts/python.exe -m pytest tests/unit/test_programme_graduation.py -v
```

## What the User Must Set

In the profile UI (or via API), the user sets:

1. **Graduation Year** (existing field): Their primary expected graduation year (e.g., 2028)
2. **Admissible Graduation Years** (new field): A set of years they are willing to apply for
   - Example: `[2028, 2029]` means "I'm eligible for both 3-year summer (2028) and 4-year placement (2029) programmes"
   - If left empty, legacy behaviour applies (stored graduation_year vs framing comparison)

The system will then:
- For a **Summer** opportunity (framing 2028): Use 2028 if 2028 ∈ admissible, else conflict
- For a **Spring Week / Year in Industry** opportunity (framing 2029): Use 2029 if 2029 ∈ admissible, else conflict

**No live profile data is modified by this migration.** The new column defaults to `[]`, preserving legacy behaviour until the user explicitly configures admissible years.