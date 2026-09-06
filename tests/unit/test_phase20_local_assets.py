from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]


def _css_tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", block, re.IGNORECASE))


def _relative_luminance(hex_colour: str) -> float:
    if len(hex_colour) == 4:
        hex_colour = "#" + "".join(character * 2 for character in hex_colour[1:])
    channels = [int(hex_colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_v2_templates_and_assets_never_request_external_hosts() -> None:
    candidates = [
        *ROOT.glob("app/templates/**/*.html"),
        *ROOT.glob("app/static/**/*"),
    ]
    candidates = [
        path for path in candidates if path.is_file() and path.suffix.casefold() in {".html", ".css", ".js"}
    ]
    assert candidates
    request_pattern = re.compile(
        r"(?:src|href)\s*=\s*['\"](?:https?:)?//|url\(\s*['\"]?(?:https?:)?//",
        re.IGNORECASE,
    )
    offenders = {
        str(path.relative_to(ROOT)): request_pattern.findall(path.read_text(encoding="utf-8"))
        for path in candidates
        if request_pattern.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}


def test_v2_keyboard_map_and_safe_form_conventions_ship_in_local_javascript() -> None:
    source = (ROOT / "app/static/js/argus-v2.js").read_text(encoding="utf-8")

    assert "data-global-search" in source
    assert "data-shortcuts" in source
    assert "data-api-form" in source
    assert "data-confirm" in source
    assert "data-confirm-form" in source
    assert "Escape" in source
    assert "g then" in source
    assert "innerHTML" not in source


def test_v2_theme_uses_local_system_stacks_and_visible_focus() -> None:
    source = (ROOT / "app/static/css/argus-v2.css").read_text(encoding="utf-8")

    assert "ui-monospace" in source
    assert "color-scheme: dark" in source
    assert '[data-theme="light"]' in source
    assert ":focus-visible" in source
    assert "ambient-a" not in source
    assert "ambient-b" not in source


def test_v2_body_action_and_status_text_meet_wcag_aa_contrast() -> None:
    """Catch palette changes that make readable or actionable text fall below 4.5:1."""

    source = (ROOT / "app/static/css/argus-v2.css").read_text(encoding="utf-8")
    dark_block = re.search(r":root\s*\{(?P<body>.*?)\}", source, re.DOTALL)
    light_block = re.search(
        r'\[data-theme="light"\]\s*\{(?P<body>.*?)\}', source, re.DOTALL
    )
    error_block = re.search(
        r"\.toast-v2\.error,\s*\.toast\.error\s*\{(?P<body>.*?)\}",
        source,
        re.DOTALL,
    )
    assert dark_block and light_block and error_block

    dark = _css_tokens(dark_block.group("body"))
    light = {**dark, **_css_tokens(light_block.group("body"))}
    semantic_pairs = (
        ("body", "bg"),
        ("muted", "bg"),
        ("dim", "bg"),
        ("accent", "bg"),
        ("accent-ink", "accent"),
        ("muted", "bg-raised"),
    )
    failures = []
    for theme_name, palette in (("dark", dark), ("light", light)):
        for foreground, background in semantic_pairs:
            ratio = _contrast_ratio(palette[foreground], palette[background])
            if ratio < 4.5:
                failures.append(f"{theme_name} {foreground}/{background}={ratio:.2f}:1")

        error_declarations = error_block.group("body")
        foreground_match = re.search(r"color:\s*(#[0-9a-f]{3,6})", error_declarations)
        assert foreground_match
        error_ratio = _contrast_ratio(foreground_match.group(1), palette["red"])
        if error_ratio < 4.5:
            failures.append(f"{theme_name} error text/red={error_ratio:.2f}:1")

    assert failures == [], "WCAG AA contrast failures: " + ", ".join(failures)


def test_v2_hidden_confirmation_is_not_visually_exposed_before_armed_selection() -> None:
    source = (ROOT / "app/static/css/argus-v2.css").read_text(encoding="utf-8")
    markup = f"""
        <style>{source}</style>
        <form class="mode-form">
          <label data-armed-confirmation hidden>Type ARM ARGUS</label>
        </form>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(markup)
        display = page.locator("[data-armed-confirmation]").evaluate(
            "node => getComputedStyle(node).display"
        )
        browser.close()

    assert display == "none"
