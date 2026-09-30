import json
from app.tracker.public_web import WebPageResult

URL = 'https://fixture.wd3.myworkdayjobs.com/en-US/Students/job/London/Finance-Placement_R123'


def payload(**updates):
    job = {'title': 'Finance Placement', 'jobReqId': 'R123', 'canApply': True,
           'posted': True, 'jobDescription': '<p>Finance degree. Graduating in 2029.</p>',
           'location': 'London', 'postedOn': 'Posted 2 Days Ago', 'endDate': '2027-01-15'}
    job.update(updates)
    return json.dumps({'jobPostingInfo': job}).encode()


def test_public_workday_detail_proves_apply_state_and_date():
    from app.tracker.workday_verification import workday_api_url, parse_workday_payload
    api = workday_api_url(URL)
    assert api == 'https://fixture.wd3.myworkdayjobs.com/wday/cxs/fixture/Students/job/London/Finance-Placement_R123'
    result = parse_workday_payload(payload(), URL, api, expected_title='Finance Placement')
    assert isinstance(result, WebPageResult)
    assert result.availability == 'open'
    assert result.deadline == '2027-01-15'
    assert result.posted_at is None  # A relative posting phrase is not a source clock.
    assert result.posted_text == 'Posted 2 Days Ago'
    assert result.source_response_hash
    assert all(item['source_url'] == api for item in result.evidence)


def test_workday_requires_explicit_flags_and_matching_requisition():
    from app.tracker.workday_verification import parse_workday_payload, workday_api_url
    for body in [payload(canApply='true'), payload(jobReqId='R999'), b'{}']:
        result = parse_workday_payload(body, URL, workday_api_url(URL), expected_title='Finance Placement')
        assert result.availability == 'unknown'
    result = parse_workday_payload(payload(posted=False), URL, workday_api_url(URL))
    assert result.availability == 'closed'


def test_public_client_uses_bounded_pinned_transport_for_workday(monkeypatch):
    from app.tracker import public_web
    from app.tracker.workday_verification import workday_api_url
    client = public_web.PublicWebClient()
    seen = []
    monkeypatch.setattr(public_web, 'resolve_public_url', lambda u: public_web.ResolvedURL(u, 'https', 'fixture.wd3.myworkdayjobs.com', 443, ('93.184.216.34',)))
    def request(resolved):
        seen.append(resolved.url)
        return 200, {'content-type': 'application/json'}, payload()
    monkeypatch.setattr(client, '_request', request)
    result = client.fetch(URL, expected_title='Finance Placement')
    assert result.availability == 'open'
    assert seen == [workday_api_url(URL)]


def test_workday_request_negotiates_json(monkeypatch):
    from app.tracker import public_web
    class Response:
        status = 200
        def getheaders(self): return [('content-type', 'application/json')]
        def read(self, *args): return b''
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, method, path, *, headers):
            assert headers['Accept'] == 'application/json'
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(public_web, '_PinnedHTTPSConnection', Connection)
    public_web.PublicWebClient()._request(public_web.ResolvedURL(
        'https://fixture.wd3.myworkdayjobs.com/wday/cxs/fixture/Students/job/a_R123',
        'https', 'fixture.wd3.myworkdayjobs.com', 443, ('93.184.216.34',)))
