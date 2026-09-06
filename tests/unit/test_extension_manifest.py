import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "extension"


def test_capture_extension_is_manifest_v3_and_local_only() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["action"]["default_popup"] == "popup.html"
    assert manifest["options_page"] == "options.html"
    assert set(manifest["permissions"]) == {"activeTab", "storage"}
    assert set(manifest["host_permissions"]) == {
        "http://127.0.0.1/*",
        "http://localhost/*",
    }
    assert "<all_urls>" not in json.dumps(manifest)


def test_popup_posts_only_user_confirmed_page_to_capture_endpoint() -> None:
    script = (EXTENSION / "popup.js").read_text(encoding="utf-8")

    assert "chrome.tabs.query" in script
    assert '"X-Argus-Token"' in script
    assert '"/api/capture"' in script
    assert "employer" in script
    assert "role_title" in script
    assert "cycle" in script
    assert "url" in script
    assert "executeScript" not in script
