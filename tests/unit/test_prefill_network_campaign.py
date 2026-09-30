"""PREFILL network boundary acceptance campaign (Agent16).

Fail-closed contract: PREFILL must not emit POST/PUT/PATCH/DELETE from page
fill, page JavaScript/autosave, or any upload path. No mutation exception
survives PREFILL; the provider flag is classification-only and does not
authorize delivery.

All browser cases use the real ``HeadedSessionWorker._route`` installed on an
owned Playwright context with two synthetic loopback HTTP servers that
independently count delivery. No Next/Submit click, no authority, no intent,
no production services. Synthetic loopback only.

Run with the explicit project interpreter and worker PYTHONPATH, e.g.::

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH='<worker>' \
      '<venv>/Scripts/python.exe' -m pytest tests/unit/test_prefill_network_campaign.py \
      -p no:cacheprovider -q
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import app
from app.services.navigator import HeadedSessionWorker

ROOT = Path(__file__).resolve().parents[2]
assert Path(app.__file__).resolve().parent == ROOT / "app"

PREFILL_BLOCK_REASON = "prefill_mutating_request_blocked"


class _PostCountingHandler(BaseHTTPRequestHandler):
    """Serve a fill page whose input listener POSTs; count delivered POSTs."""

    posts: list[bytes] = []
    gets: int = 0

    def log_message(self, *args):  # pragma: no cover - silence test servers
        pass

    def do_GET(self):
        type(self).gets += 1
        body = (
            b'<input id="name"><script>'
            b'document.querySelector("input").addEventListener("input",()=>'
            b'fetch(window.sink,{method:"POST",body:"synthetic-name"})'
            b'.catch(()=>{}).finally(()=>window.finished=true));'
            b"</script>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        type(self).posts.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def _servers(handler=_PostCountingHandler):
    first = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    second = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (first, second)
    ]
    for thread in threads:
        thread.start()
    return (first, second), threads


class _PlainFillHandler(BaseHTTPRequestHandler):
    """Serve a fill page with no page JavaScript; count delivered POSTs."""

    posts: list[bytes] = []

    def log_message(self, *args):  # pragma: no cover - silence test servers
        pass

    def do_GET(self):
        body = b'<input id="name">'
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        type(self).posts.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def _stop(servers, threads):
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=3)


def _worker(*, mode, origin, run_scoped=True, summary=None, **kwargs):
    worker = HeadedSessionWorker(
        session_id=f"prefill-campaign-{mode}",
        application_id="app-1",
        mode=mode,
        url=origin,
        summary=summary or {"provider": "synthetic"},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=True,
        allowlist=frozenset({"127.0.0.1"}),
        allowlist_is_run_scoped=run_scoped,
        **kwargs,
    )
    worker.owner_thread_id = threading.get_ident()
    return worker


def _run_fill_page(worker, *, origin, sink):
    """Drive a real fill through the real guard; return (server_posts, worker)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers="block")
            page = context.new_page()
            worker._page = page

            def guard(route):
                assert route.request.url.startswith("http://127.0.0.1:"), (
                    "non-loopback network forbidden"
                )
                worker._route(route)

            context.route("**/*", guard)
            page.goto(origin, timeout=10000)
            page.evaluate("(sink)=>window.sink=sink", sink)
            page.fill("#name", "Synthetic Candidate")
            page.wait_for_function("window.finished === true", timeout=5000)
            assert page.input_value("#name") == "Synthetic Candidate"
        finally:
            browser.close()
    return worker


def test_prefill_same_origin_post_blocked_browser():
    """RED on old code: PREFILL same-origin fetch POST was delivered (1)."""
    _PostCountingHandler.posts = []
    _PostCountingHandler.gets = 0
    servers, threads = _servers()
    try:
        origin = f"http://127.0.0.1:{servers[0].server_port}"
        sink = f"http://127.0.0.1:{servers[0].server_port}/submit"
        worker = _worker(mode="prefill", origin=origin)
        _run_fill_page(worker, origin=origin, sink=sink)
        assert len(_PostCountingHandler.posts) == 0, (
            "PREFILL must not deliver a same-origin POST from page JavaScript"
        )
        mutations = [
            record
            for record in worker._egress_records
            if record.get("method") == "POST"
        ]
        assert len(mutations) == 1
        assert mutations[0]["allowed"] is False
        assert mutations[0]["fatal"] is True
        assert mutations[0]["reason"] == PREFILL_BLOCK_REASON
        print(json.dumps({"mode": "prefill", "server_posts": 0, "reason": PREFILL_BLOCK_REASON}))
    finally:
        _stop(servers, threads)


def test_review_same_origin_post_still_blocked_browser():
    _PostCountingHandler.posts = []
    servers, threads = _servers()
    try:
        origin = f"http://127.0.0.1:{servers[0].server_port}"
        sink = f"http://127.0.0.1:{servers[0].server_port}/submit"
        worker = _worker(mode="review", origin=origin)
        _run_fill_page(worker, origin=origin, sink=sink)
        assert len(_PostCountingHandler.posts) == 0
        mutations = [
            record
            for record in worker._egress_records
            if record.get("method") == "POST"
        ]
        assert len(mutations) == 1
        assert mutations[0]["allowed"] is False
        assert mutations[0]["reason"] == "read_only_data_bearing_request"
    finally:
        _stop(servers, threads)


def test_prefill_foreign_origin_post_blocked_browser():
    _PostCountingHandler.posts = []
    servers, threads = _servers()
    try:
        origin = f"http://127.0.0.1:{servers[0].server_port}"
        sink = f"http://127.0.0.1:{servers[1].server_port}/submit"
        worker = _worker(mode="prefill", origin=origin)
        _run_fill_page(worker, origin=origin, sink=sink)
        assert len(_PostCountingHandler.posts) == 0
        mutations = [
            record
            for record in worker._egress_records
            if record.get("method") == "POST"
        ]
        assert len(mutations) == 1
        assert mutations[0]["allowed"] is False
        assert mutations[0]["reason"] == "data_bearing_unapproved_origin"
    finally:
        _stop(servers, threads)


def test_prefill_readonly_get_fill_navigation_allowed_browser():
    """Read-only GET/HEAD/OPTIONS navigation required for fill still works."""
    _PostCountingHandler.posts = []
    _PostCountingHandler.gets = 0
    _PlainFillHandler.posts = []
    servers, threads = _servers(_PlainFillHandler)
    try:
        from playwright.sync_api import sync_playwright

        origin = f"http://127.0.0.1:{servers[0].server_port}"
        worker = _worker(mode="prefill", origin=origin)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(service_workers="block")
                page = context.new_page()
                worker._page = page

                def guard(route):
                    assert route.request.url.startswith("http://127.0.0.1:"), (
                        "non-loopback network forbidden"
                    )
                    worker._route(route)

                context.route("**/*", guard)
                page.goto(origin, timeout=10000)
                page.fill("#name", "Synthetic Candidate")
                assert page.input_value("#name") == "Synthetic Candidate"
            finally:
                browser.close()
        assert len(_PlainFillHandler.posts) == 0
        allowed_gets = [
            record
            for record in worker._egress_records
            if record.get("method") == "GET" and record.get("allowed") is True
        ]
        assert allowed_gets, "read-only GET navigation required for fill must stay allowed"
    finally:
        _stop(servers, threads)


class _FakeRequest:
    def __init__(self, *, url, method="GET", post_data="", headers=None,
                 resource_type="xhr", navigation=False):
        self.url = url
        self.method = method
        self.post_data = post_data
        self.headers = headers or {}
        self.resource_type = resource_type
        self.is_navigation_request = navigation
        self.frame = None

    def redirected_from(self):
        return None


class _FakeRoute:
    def __init__(self, request):
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self):
        self.continued += 1

    def abort(self, reason=""):
        self.aborted.append(reason)


def _drive_synthetic(*, mode, method, url, page_url=None, run_scoped=True,
                     post_data="x", headers=None, journey_executor=None,
                     summary=None):
    worker = _worker(mode=mode, origin=page_url or url, run_scoped=run_scoped,
                     journey_executor=journey_executor, summary=summary)
    route = _FakeRoute(_FakeRequest(url=url, method=method, post_data=post_data,
                                    headers=headers, navigation=False))
    worker._route(route)
    records = [
        record for record in worker._egress_records
        if record.get("method") == str(method).upper()
    ]
    assert records, f"expected an egress record for {method}"
    return route, records[-1]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_prefill_mutating_methods_blocked_same_origin(method):
    """Every mutating method is fail-closed under PREFILL, exact rejection."""
    url = "http://127.0.0.1:8191/apply"
    route, record = _drive_synthetic(
        mode="prefill", method=method, url=f"{url}/submit",
    )
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == PREFILL_BLOCK_REASON


def test_prefill_run_scoped_upload_post_blocked_with_explicit_diagnostic():
    """A mutating upload POST under run-scoped PREFILL is blocked, not retagged.

    The worker page sits on the form origin while the POST targets the
    provider bucket, so the cross-origin guard refuses delivery. The refusal
    keeps its origin reason and is never relabelled as an authorized upload:
    an upload that needs a mutating request under run-scoped PREFILL must go
    through handoff/explicit diagnostics, never a claimed upload.
    """
    route, record = _drive_synthetic(
        mode="prefill",
        method="POST",
        url="https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/",
        page_url="https://job-boards.greenhouse.io/point72/jobs/8423978002",
        post_data='------argus\r\nContent-Disposition: form-data; name="file"; filename="cv.pdf"\r\n\r\n',
        headers={
            "content-type": "multipart/form-data; boundary=----argus",
            "origin": "https://job-boards.greenhouse.io",
        },
        journey_executor=SimpleNamespace(active_document_upload={
            "approved": True, "kind": "document.cv",
            "path": r"C:\approved\cv.pdf", "sha256": "b" * 64,
        }),
    )
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"
    assert "upload" not in record["reason"], (
        "unauthorized writes must not be retagged as uploads"
    )


def test_prefill_exact_approved_s3_branch_blocked_not_bypassed():
    """RED proof: exact bound approved Greenhouse S3 upload under run_scoped=False
    was allowed by the provider_document_upload_allowed bypass; must be blocked
    with prefill_mutating_request_blocked after fix.

    This test uses the EXACT bound conditions from the navigator guard:
    - run_scoped=False
    - provider greenhouse
    - exact bucket host grnhse-prod-jben-us-east-1.s3.amazonaws.com
    - verified request origin matching page
    - POST xhr multipart with approved document filename
    - approved active kind and valid sha256
    - content-type multipart/form-data

    Synthetic route interception only; NEVER actual S3 request.
    Must distinguish old branch delivery (allowed=True, reason=greenhouse_bound_approved_document_upload)
    from generic cross-origin rejection (reason=data_bearing_unapproved_origin).
    """
    route, record = _drive_synthetic(
        mode="prefill",
        method="POST",
        url="https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/",
        page_url="https://job-boards.greenhouse.io/point72/jobs/8423978002",
        post_data='------argus\r\nContent-Disposition: form-data; name="file"; filename="cv.pdf"\r\n\r\n',
        headers={
            "content-type": "multipart/form-data; boundary=----argus",
            "origin": "https://job-boards.greenhouse.io",
        },
        journey_executor=SimpleNamespace(active_document_upload={
            "approved": True, "kind": "document.cv",
            "path": r"C:\approved\cv.pdf", "sha256": "b" * 64,
        }),
        run_scoped=False,
        summary={"provider": "greenhouse"},
    )
    # OLD CODE (bypass active): route.continued == 1, record["allowed"] == True,
    # record["reason"] == "greenhouse_bound_approved_document_upload"
    # NEW CODE (bypass removed): route.continued == 0, record["allowed"] == False,
    # record["reason"] == "prefill_mutating_request_blocked"
    assert route.continued == 0, (
        "Exact approved S3 branch must be blocked, not bypassed"
    )
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == PREFILL_BLOCK_REASON, (
        f"Must be blocked with {PREFILL_BLOCK_REASON}, not greenhouse_bound_approved_document_upload"
    )


def test_submit_same_origin_post_not_globally_denied():
    """No new global deny for SUBMIT: action-time authorized POST still routes.
    Route-level allowance only; does not prove action-time authority, user intent,
    or final submission confirmation."""
    route, record = _drive_synthetic(
        mode="submit", method="POST", url="http://127.0.0.1:8191/apply/submit",
    )
    assert route.continued == 1
    assert route.aborted == []
    assert record["allowed"] is True
    assert record["reason"] == "approved_origin"


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_prefill_readonly_methods_allowed_synthetic(method):
    """PREFILL read-only GET/HEAD/OPTIONS are allowed; no mutation delivered."""
    route, record = _drive_synthetic(
        mode="prefill", method=method, url="http://127.0.0.1:8191/form",
        post_data="",
    )
    assert route.continued == 1
    assert route.aborted == []
    assert record["allowed"] is True
    assert record["fatal"] is False
    assert record["reason"] in ("approved_origin", "passive_asset", "other")


@pytest.mark.parametrize("mode", ["review", "inspect", "dry_run"])
def test_readonly_modes_preserve_data_bearing_refusal(mode):
    """REVIEW/inspect/dry_run keep read_only_data_bearing_request for same-origin candidate POST."""
    route, record = _drive_synthetic(
        mode=mode, method="POST", url="http://127.0.0.1:8191/apply",
        post_data="candidate=data",
    )
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "read_only_data_bearing_request"


class _ExactS3Fixture:
    """Bound synthetic fixture for exact approved Greenhouse S3 upload conditions."""
    url = "https://grnhse-prod-jben-us-east-1.s3.amazonaws.com/"
    page_url = "https://job-boards.greenhouse.io/point72/jobs/8423978002"
    method = "POST"
    content_type = "multipart/form-data; boundary=----argus"
    origin = "https://job-boards.greenhouse.io"
    filename = "cv.pdf"
    kind = "document.cv"
    sha256 = "b" * 64
    post_data = (
        '------argus\r\nContent-Disposition: form-data; name="file"; filename="cv.pdf"\r\n\r\n'
    )
    headers = {
        "content-type": content_type,
        "origin": origin,
    }
    journey_executor = SimpleNamespace(active_document_upload={
        "approved": True, "kind": kind,
        "path": rf"C:\approved\{filename}", "sha256": sha256,
    })
    run_scoped = False
    summary = {"provider": "greenhouse"}


def _drive_exact_s3(*, filename=None, sha256=None, kind=None, post_data=None,
                    headers=None, content_type=None, method=None):
    """Drive synthetic route with exact S3 fixture, allowing targeted mutations."""
    fx = _ExactS3Fixture
    active_upload = dict(fx.journey_executor.active_document_upload)
    if filename is not None:
        active_upload["path"] = rf"C:\approved\{filename}"
    if sha256 is not None:
        active_upload["sha256"] = sha256
    if kind is not None:
        active_upload["kind"] = kind
    journey_executor = SimpleNamespace(active_document_upload=active_upload)

    pd = post_data if post_data is not None else fx.post_data
    hdrs = headers if headers is not None else fx.headers
    ct = content_type if content_type is not None else fx.content_type
    mtd = method if method is not None else fx.method

    if filename is not None and "filename=" not in pd:
        pd = pd.replace('filename="cv.pdf"', f'filename="{filename}"')
    if content_type is not None:
        hdrs = dict(hdrs)
        hdrs["content-type"] = content_type

    route, record = _drive_synthetic(
        mode="prefill",
        method=mtd,
        url=fx.url,
        page_url=fx.page_url,
        post_data=pd,
        headers=hdrs,
        journey_executor=journey_executor,
        run_scoped=fx.run_scoped,
        summary=fx.summary,
    )
    return route, record


def test_prefill_exact_approved_s3_absent_filename_blocked():
    """Exact approved S3 branch: absent filename in multipart must be blocked."""
    route, record = _drive_exact_s3(post_data='------argus\r\nContent-Disposition: form-data; name="file"\r\n\r\n')
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"


def test_prefill_exact_approved_s3_wrong_filename_blocked():
    """Exact approved S3 branch: filename mismatch must be blocked."""
    route, record = _drive_exact_s3(post_data='------argus\r\nContent-Disposition: form-data; name="file"; filename="other.pdf"\r\n\r\n')
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"


def test_prefill_exact_approved_s3_missing_hash_blocked():
    """Exact approved S3 branch: missing sha256 must be blocked."""
    route, record = _drive_exact_s3(sha256="")
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"


def test_prefill_exact_approved_s3_malformed_hash_blocked():
    """Exact approved S3 branch: malformed sha256 must be blocked."""
    route, record = _drive_exact_s3(sha256="not-a-valid-hex")
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"


def test_prefill_exact_approved_s3_wrong_kind_blocked():
    """Exact approved S3 branch: unsupported kind must be blocked."""
    route, record = _drive_exact_s3(kind="document.other")
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"


def test_prefill_exact_approved_s3_wrong_content_type_blocked():
    """Exact approved S3 branch: non-multipart content-type must be blocked."""
    route, record = _drive_exact_s3(content_type="application/json", post_data='{}')
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == "data_bearing_unapproved_origin"


def test_prefill_upload_outcome_handoff_manifest_gap():
    """Owner-scoped synthetic test exposing upload-outcome/handoff/manifest caller-proof gap.

    This test exercises the real call/result shape for an upload attempt under PREFILL.
    It does NOT fabricate a success dict. The guard blocks with prefill_mutating_request_blocked,
    but the caller/handoff/manifest layer may still claim a successful upload — that gap
    remains an unresolved caller-proof issue requiring owner-scoped integration test or escalation.
    """
    # Drive the exact bound approved S3 branch — guard blocks it
    route, record = _drive_exact_s3()
    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert record["allowed"] is False
    assert record["fatal"] is True
    assert record["reason"] == PREFILL_BLOCK_REASON

    # The gap: no journey_executor/handoff code path is exercised here.
    # A real upload would require journey_executor.active_document_upload to be processed
    # through the upload/handoff/manifest pipeline. That code is outside this worker's ownership.
    # This test documents the exact blocked shape; any manifest claiming upload success
    # would be a false-positive defect in the caller layer, not the guard.
    print(json.dumps({
        "test": "upload_outcome_handoff_manifest_gap",
        "guard_reason": record["reason"],
        "route_aborted": route.aborted,
        "gap": "caller/handoff/manifest layer not exercised; upload success claim would be false positive",
        "status": "UNRESOLVED_OWNER_SCOPED_TEST_REQUIRED"
    }))
