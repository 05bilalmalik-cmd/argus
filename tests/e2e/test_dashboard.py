from playwright.sync_api import sync_playwright


def test_dashboard_renders_without_console_errors_and_mobile_navigation(live_server) -> None:
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.goto(live_server.base_url, wait_until="networkidle")

        assert page.get_by_role("heading", name="Command Centre").is_visible()
        assert page.get_by_text("Your applications,").is_visible()
        assert page.get_by_text("GUARDRAILS ACTIVE").is_visible()
        assert errors == []

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(live_server.base_url, wait_until="networkidle")
        mobile.get_by_role("button", name="Open navigation").click()
        assert mobile.get_by_role("link", name="ATS Laboratory").is_visible()
        browser.close()


def test_mobile_pipeline_rows_do_not_require_horizontal_scrolling(live_server) -> None:
    import httpx

    opportunity = httpx.post(
        f"{live_server.base_url}/api/opportunities",
        json={
            "employer": "ARGUS Test Capital",
            "role_title": "Summer Analyst",
            "division": "Investment Banking",
            "location": "London",
            "cycle": "2027",
            "url": f"{live_server.base_url}/lab/ats/standard?mobile=1",
            "sponsorship_supported": True,
        },
        timeout=10,
    )
    opportunity.raise_for_status()
    evaluated = httpx.post(
        f"{live_server.base_url}/api/opportunities/{opportunity.json()['id']}/evaluate",
        timeout=10,
    )
    evaluated.raise_for_status()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(live_server.base_url, wait_until="networkidle")
        pipeline = page.locator(".pipeline-table")

        assert pipeline.count() == 1
        assert pipeline.evaluate("element => element.scrollWidth <= element.clientWidth") is True
        row = page.locator(".pipeline-table tbody tr").first
        assert row.evaluate("element => getComputedStyle(element).display") == "grid"
        action_box = row.locator("td").nth(3).bounding_box()
        assert action_box is not None
        assert action_box["width"] >= 180
        browser.close()


def test_conflict_rules_are_readable_without_mobile_horizontal_scroll(live_server) -> None:
    import httpx

    created = httpx.post(
        f"{live_server.base_url}/api/conflict-rules",
        json={
            "employer_pattern": "Example Bank*",
            "cycle": "2027",
            "max_applications": 1,
            "exclusive_groups": "ibd,markets,asset_management",
            "notes": "Published one-application rule",
        },
        timeout=10,
    )
    created.raise_for_status()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"{live_server.base_url}/settings", wait_until="networkidle")

        assert page.get_by_role("heading", name="Employer application rules").is_visible()
        assert page.get_by_text("Example Bank*").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True
        assert page.locator(".rule-layout").evaluate(
            "element => getComputedStyle(element).gridTemplateColumns.split(' ').length === 1"
        ) is True
        browser.close()
