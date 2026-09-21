from app.tracker import sources


def page(n, total=2, pages=2, job_id=None):
    return f'''<p>Page {n} of {pages} · {total} openings</p><table><thead><tr>
    <th>Company</th><th>Role</th><th>Programme</th><th>Location</th><th>Deadline</th><th>Apply</th>
    </tr></thead><tbody><tr><td>Firm</td><td>Summer Internship {job_id or n}</td>
    <td>Summer internship</td><td>London</td><td>-</td>
    <td><a href="https://firm.example/jobs/{job_id or n}">Apply</a></td></tr></tbody></table>'''


def test_complete_public_pages_are_collected(monkeypatch):
    visited = []
    def fetch(url):
        visited.append(url)
        return page(2 if 'page=2' in url else 1)
    monkeypatch.setattr(sources, 'http_get_text', fetch)
    result = sources.simplytk_source()
    assert len(visited) == 2
    assert len(result.listings) == 2
    assert result.status == 'ok'
    assert not result.error


def test_repeated_page_does_not_claim_completeness(monkeypatch):
    monkeypatch.setattr(sources, 'http_get_text', lambda url: page(1))
    result = sources.simplytk_source()
    assert result.status == 'partial'
    assert len(result.listings) == 1


def test_failed_later_page_retains_first_page(monkeypatch):
    def fetch(url):
        if 'page=2' in url:
            raise TimeoutError('fixture timeout')
        return page(1)
    monkeypatch.setattr(sources, 'http_get_text', fetch)
    result = sources.simplytk_source()
    assert result.status == 'partial'
    assert len(result.listings) == 1
    assert 'TimeoutError' in result.error


def test_advertised_count_drift_remains_partial(monkeypatch):
    monkeypatch.setattr(sources, 'http_get_text', lambda url: page(2, total=3) if 'page=2' in url else page(1))
    assert sources.simplytk_source().status == 'partial'
