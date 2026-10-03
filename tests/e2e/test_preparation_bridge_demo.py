"""Acceptance-oracle tests; these fixtures are NOT live bridge evidence."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _demo():
    spec = importlib.util.find_spec('scripts.verify_preparation_bridge')
    assert spec is not None, 'reusable preparation bridge verifier is missing'
    from scripts import verify_preparation_bridge
    return verify_preparation_bridge


def test_positive_fixture_is_single_page_and_staged_boundary_is_separate():
    from scripts.preparation_bridge_demo_server import render_lab_form
    assert '>Next</button>' not in render_lab_form(staged=False)
    assert '>Next</button>' in render_lab_form(staged=True)
    assert 'Submit application' in render_lab_form(staged=False)


def test_oracle_rejects_missing_owner_browser_evidence():
    with pytest.raises(AssertionError, match='owner'):
        _demo().assert_browser_proof({}, expected_fields={}, expected_cv_sha256='a' * 64)


@pytest.mark.parametrize('corrupt', [
    {'next_clicks': 1}, {'submit_clicks': 1}, {'submit_events': 1},
    {'lab_posts': 1}, {'lab_submissions': 1}, {'prepare_calls': 2},
    {'browser_mutating_requests': ['POST']}, {'navigator_owner_thread_id': 2},
    {'files': {'cv': [{'size': 1, 'sha256': 'b' * 64}]}},
    {'worker_alive': False}, {'page_url_matches': False}, {'actual_headless': True},
    {'external_browser_requests': ['GET']}, {'fields': {'first_name': 'Wrong'}},
])
def test_oracle_rejects_false_positive(corrupt):
    proof = dict(owner_thread_id=1, navigator_owner_thread_id=1, worker_alive=True,
                 actual_headless=False,
                 prepare_calls=1, next_clicks=0, submit_clicks=0, submit_events=0,
                 browser_mutating_requests=[], external_browser_requests=[], lab_posts=0,
                 lab_submissions=0, page_url_matches=True, fields={'first_name': 'Synthetic'},
                 files={'cv': [{'size': 1, 'sha256': 'a' * 64}]})
    proof.update(corrupt)
    with pytest.raises(AssertionError):
        _demo().assert_browser_proof(proof, expected_fields={'first_name': 'Synthetic'},
                                    expected_cv_sha256='a' * 64)


def test_native_refusal_envelope_is_protocol_not_transport_error():
    import json
    from app.bridge.protocol import refusal
    reply = refusal('NO_APPROVAL')
    assert _demo().decode_hermes_result(json.dumps({'error': json.dumps(reply)})) == reply
    with pytest.raises((AssertionError, ValueError)):
        _demo().decode_hermes_result({'error': 'connection closed'})


def test_real_production_prefill_observer(tmp_path):
    """Real source PREFILL/browser only; not a claim of bridge acceptance."""
    import json
    import subprocess
    import sys
    from scripts.verify import safe_environment
    root = Path(__file__).resolve().parents[2]
    env = safe_environment(root)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, str(root / 'scripts/verify_preparation_bridge.py'),
                             '--observer-check', '--interpreter', sys.executable,
                             '--evidence-dir', str(tmp_path / 'evidence')],
                            cwd=root, env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    manifest = json.loads((Path(summary['evidence']) / 'manifest.json').read_text())
    assert manifest['scope'] == 'observer-only'
    assert manifest['result']['bridge_verified'] is False
    assert manifest['result']['observer_verified'] is True
