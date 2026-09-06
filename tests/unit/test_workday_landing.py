"""Unit tests for the Workday landing-flow adapter.

The flow itself is browser-driven; these tests cover the deterministic parts:
detection (host + markup), selector tables, and the step-walking logic of
submit() against a fake page object. The live click-through is covered by an
opt-in network test.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.automation.adapters.workday import WorkdayAdapter


class TestDetection:
    def test_matches_workday_hosts(self):
        assert WorkdayAdapter.matches("https://ardian.wd103.myworkdayjobs.com/x")
        assert WorkdayAdapter.matches("https://tritonpartners.wd3.myworkdayjobs.com/y")
        assert WorkdayAdapter.matches("https://blackstone.wd1.myworkdaysite.com/z")

    def test_rejects_other_hosts(self):
        assert not WorkdayAdapter.matches("https://boards.greenhouse.io/acme")
        assert not WorkdayAdapter.matches("https://example.com/job/1")

    def test_matches_markup_marker_without_trusted_host(self):
        assert not WorkdayAdapter.matches(
            "https://careers.example.com/1",
            '<div data-ats="workday">',
        )


class TestSubmitStepWalking:
    @staticmethod
    def _page_with_sequence(states):
        """Fake page: each poll returns the next state dict.

        state keys: submit_visible, next_visible. is_visible/click recorded.
        """
        calls = {"clicks": [], "index": 0}

        page = MagicMock()
        page.url = "https://ardian.wd103.myworkdayjobs.com/apply"
        page.wait_for_load_state = MagicMock()
        page.wait_for_timeout = MagicMock()
        page.evaluate = MagicMock(return_value="step")

        def locator_side_effect(selector):
            entry = MagicMock()
            state = states[min(calls["index"], len(states) - 1)]
            is_submit = selector == '[data-automation-id="submitButton"]'
            is_next = selector.startswith('[data-automation-id="submitNextButton"]') or selector == 'button:has-text("Next")'
            present = (is_submit and state.get("submit_visible")) or (
                is_next and state.get("next_visible")
            )

            def count():
                return 1 if present else 0

            def first_is_visible():
                return bool(present)

            def click(timeout=10000):
                calls["clicks"].append(selector)
                calls["index"] = min(calls["index"] + 1, len(states) - 1)

            entry.count = count
            entry.first = MagicMock()
            entry.first.is_visible = first_is_visible
            entry.first.click = click

            # is_final_submit_visible iterates nth(index) entries.
            def nth(_i):
                item = MagicMock()
                item.is_visible = MagicMock(return_value=bool(present))
                item.is_enabled = MagicMock(return_value=True)
                item.click = click
                return item

            entry.nth = nth
            return entry

        page.locator = locator_side_effect
        return page, calls

    def test_submit_walks_next_steps_then_clicks_final(self):
        states = [
            {"submit_visible": False, "next_visible": True},
            {"submit_visible": False, "next_visible": True},
            {"submit_visible": True, "next_visible": False},
        ]
        page, calls = self._page_with_sequence(states)
        adapter = WorkdayAdapter()
        adapter.submit(page)
        clicked = [c for c in calls["clicks"]]
        assert clicked[0].startswith('[data-automation-id="submitNextButton"')
        assert clicked[-1] == '[data-automation-id="submitButton"]'

    def test_submit_raises_when_no_controls_ever(self):
        page, _calls = self._page_with_sequence(
            [{"submit_visible": False, "next_visible": False}]
        )
        adapter = WorkdayAdapter()
        with pytest.raises(RuntimeError, match="No visible submission control"):
            adapter.submit(page)


class TestFlowHelpers:
    def test_enter_flow_returns_false_when_no_landing(self):
        page = MagicMock()
        page.locator = lambda sel: _absent_locator()
        adapter = WorkdayAdapter()
        assert adapter.enter_application_flow(page) is False


def _absent_locator():
    entry = MagicMock()
    entry.count = lambda: 0
    entry.first = MagicMock()
    entry.first.is_visible = lambda: False
    return entry
