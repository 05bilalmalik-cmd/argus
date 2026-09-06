import json

import pytest

from app.automation.classifier import ClassificationError, OllamaClassifier
from app.domain.questions import CanonicalKey, FormQuestion, Sensitivity


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return FakeResponse(self.payload)


def test_ollama_classifier_uses_allowlisted_json_schema_and_returns_mapping() -> None:
    client = FakeClient(
        {
            "response": json.dumps(
                {
                    "canonical_key": "education.degree",
                    "confidence": 0.82,
                    "sensitivity": "standard",
                    "reason": "The label asks for the degree course.",
                }
            )
        }
    )
    classifier = OllamaClassifier("http://127.0.0.1:11434", "qwen3:8b", client=client)

    mapping = classifier.classify(FormQuestion("Course of study", "text", required=True))

    assert mapping.canonical_key == CanonicalKey.DEGREE
    assert mapping.sensitivity == Sensitivity.STANDARD
    url, body, timeout = client.calls[0]
    assert url == "http://127.0.0.1:11434/api/generate"
    assert body["stream"] is False
    assert set(body["format"]["properties"]["canonical_key"]["enum"]) == {
        key.value for key in CanonicalKey
    }
    assert "Do not answer" in body["prompt"]
    assert timeout == 8.0


def test_ollama_classifier_rejects_key_outside_allowlist() -> None:
    client = FakeClient(
        {
            "response": json.dumps(
                {
                    "canonical_key": "invented.secret",
                    "confidence": 0.99,
                    "sensitivity": "standard",
                    "reason": "invalid",
                }
            )
        }
    )
    classifier = OllamaClassifier("http://127.0.0.1:11434", "qwen3:8b", client=client)

    with pytest.raises(ClassificationError, match="schema validation"):
        classifier.classify(FormQuestion("Unfamiliar required field", "text", required=True))
