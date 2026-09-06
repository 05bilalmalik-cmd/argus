"""Deterministic source and release-artifact privacy scanner.

The scanner intentionally checks only high-signal representations of a
candidate profile: values assigned to identity fields, email/phone-shaped
values, user-home paths, and legal-status phrases.  Generic words such as
``profile`` or ``sponsorship`` are not findings.  Synthetic demo values and
documentation domains are allowlisted in ``packaging/privacy_scan_config.json``.

The command prints finding locations and categories, never the matched value.
It can therefore be used in release verification without echoing a secret into
logs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class PrivacyConfig:
    source_roots: tuple[str, ...]
    artifact_runtime_roots: tuple[str, ...]
    excluded_paths: frozenset[str]
    synthetic_name_values: frozenset[str]
    synthetic_email_domains: frozenset[str]
    synthetic_legal_markers: tuple[str, ...]
    text_suffixes: frozenset[str]
    forbidden_name_tokens: frozenset[str]
    forbidden_content_tokens: frozenset[str]
    baseline_sha256: str


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    path: str
    category: str
    line: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9-]+\.)+[A-Z]{2,63}\b",
    re.IGNORECASE,
)
_PHONE_PATTERN = re.compile(
    r"(?<!\w)\+?\d(?:[\d().\s-]{7,}\d)(?!\w)"
)
_NAME_ASSIGNMENT_PATTERN = re.compile(
    r"(?:\b(?:first_name|last_name|preferred_name)\b|"
    r"['\"](?:first_name|last_name|preferred_name)['\"])\s*[:=]\s*"
    r"(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)",
)
_PHONE_FIELD_PATTERN = re.compile(
    r"(?:\b(?:phone|mobile|telephone|tel|contact_phone)\b|"
    r"['\"](?:phone|mobile|telephone|tel|contact_phone)['\"])\s*[:=]\s*"
    r"(?P<quote>['\"])(?P<value>\+?[\d().\s-]{9,})(?P=quote)",
    re.IGNORECASE,
)
_PHONE_FIELD_NUMBER_PATTERN = re.compile(
    r"(?:\b(?:phone|mobile|telephone|tel|contact_phone)\b|"
    r"['\"](?:phone|mobile|telephone|tel|contact_phone)['\"])\s*[:=]\s*"
    r"(?P<value>\+?\d{9,15})(?!\w)",
    re.IGNORECASE,
)
_USER_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+"
    r"|/Users/[^/\s\"']+|/home/[^/\s\"']+)",
    re.IGNORECASE,
)
_LEGAL_PATTERNS = (
    re.compile(r"\b(?:[A-Z]{2,3}|United\s+Kingdom|United\s+States)\s+citizen\b", re.I),
    re.compile(
        r"\bright\s+to\s+work\b.{0,100}\b(?:sponsor(?:ship)?|visa|citizen)\b",
        re.I | re.S,
    ),
    re.compile(
        r"\b(?:no|without|does\s+not\s+require|not\s+require)\s+"
        r"sponsor(?:ship|ed)?\b",
        re.I,
    ),
)
_BINARY_SKIP_SUFFIXES = frozenset()

# This is deliberately embedded in code, rather than trusted from the JSON
# policy file. A checked-in policy may only tighten these rules. Its
# ``baseline_sha256`` must match this exact payload, so changing the trusted
# baseline requires a code review and a code change.
_BUILTIN_PRIVACY_BASELINE = {
    "source_roots": (
        "app", "scripts", "extension", "packaging", "launcher.py", "tests",
        "docs", "README.md", "SECURITY.md", "OPERATIONS.md", "VERIFICATION.md",
        "pyproject.toml", "requirements.txt",
    ),
    "artifact_runtime_roots": (
        "app", "scripts", "extension", "packaging", "launcher.py", "tests",
        "docs", "README.md", "SECURITY.md", "OPERATIONS.md", "VERIFICATION.md",
        "pyproject.toml", "requirements.txt",
    ),
    "excluded_paths": (
        "scripts/privacy_scan.py", "__pycache__", "docs/argus_change_log_2026-08-23.md",
        "docs/argus_orchestration_log_2026-08-24.md", "docs/argus_release_evidence_0.2.0.json",
        "docs/engineering_log_application_engine_2026-08-24.md",
        "docs/implementation_log_target_pipeline_2026-08-24.md", "docs/upgrade_0.2.0.md",
        "docs/provider_egress_hardening_report_2026-08-25.md", "docs/superpowers",
        "docs/ui_session_hardening_report_2026-08-25.md",
        "docs/target_provider_review_fix2_2026-08-25.md",
        "tests/unit/test_privacy_scan.py", "tests/unit/test_mode_and_egress.py",
        "tests/unit/test_allowlist_matching.py", "tests/integration/test_release_tooling_task8b.py",
        "tests/integration/test_candidate_services.py", "tests/integration/test_cli.py",
        "tests/integration/test_mail_service.py", "tests/integration/test_repositories.py",
        "tests/unit/test_target_resolution_service.py", "release-evidence", "test-evidence",
        "dist", "backups", ".hermes", ".venv",
    ),
    "synthetic_name_values": ("demo", "candidate"),
    "synthetic_email_domains": ("argus.local", "example.com", "example.org", "example.test"),
    "synthetic_legal_markers": ("lab-only demo", "synthetic fixture"),
    "text_suffixes": (
        ".css", ".cfg", ".csv", ".html", ".ini", ".js", ".json", ".md", ".py",
        ".ps1", ".sh", ".spec", ".ts", ".tsx", ".toml", ".txt", ".xml", ".yaml",
        ".yml", ".bin", ".dat",
    ),
    "forbidden_name_tokens": (
        "cookie", "keylog", "sslkeylogfile", "screenshot", "candidate", "resume",
        "old-release", "release-candidate", "backup",
    ),
    "forbidden_content_tokens": ("TRACKR_COOKIE", "SUBMISSION_TOKEN", "LIVE_SUBMIT", "SSLKEYLOGFILE"),
}


def _normalise_policy_payload(payload: Mapping[str, object]) -> dict[str, list[str]]:
    def values(key: str) -> list[str]:
        raw = payload.get(key, ())
        if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"privacy scan config field {key!r} must be a string list")
        return sorted({item for item in raw if item}, key=str.casefold)

    return {
        "source_roots": values("source_roots"),
        "artifact_runtime_roots": sorted(
            {item.rstrip("/").casefold() for item in values("artifact_runtime_roots")},
            key=str.casefold,
        ),
        "excluded_paths": sorted(
            {item.replace("\\", "/").casefold() for item in values("excluded_paths")},
            key=str.casefold,
        ),
        "synthetic_name_values": sorted({item.casefold() for item in values("synthetic_name_values")}, key=str.casefold),
        "synthetic_email_domains": sorted({item.casefold().lstrip("@") for item in values("synthetic_email_domains")}, key=str.casefold),
        "synthetic_legal_markers": sorted({item.casefold() for item in values("synthetic_legal_markers")}, key=str.casefold),
        "text_suffixes": sorted({item.casefold() if item.startswith(".") else "." + item.casefold() for item in values("text_suffixes")}, key=str.casefold),
        "forbidden_name_tokens": sorted({item.casefold() for item in values("forbidden_name_tokens")}, key=str.casefold),
        "forbidden_content_tokens": sorted({item.casefold() for item in values("forbidden_content_tokens")}, key=str.casefold),
    }


def _policy_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(_normalise_policy_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_BUILTIN_PRIVACY_BASELINE_SHA256 = _policy_digest(_BUILTIN_PRIVACY_BASELINE)


def _decode_payload(raw: bytes) -> str:
    """Decode text, UTF-16, and printable runs without echoing raw bytes."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16", "utf-8-sig")
    elif len(raw) >= 8 and (raw[1::2].count(0) >= len(raw[1::2]) // 2 or raw[0::2].count(0) >= len(raw[0::2]) // 2):
        encodings = ("utf-16-le", "utf-16-be", "utf-8-sig")
    else:
        encodings = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be")
    decoded = ""
    for encoding in encodings:
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    chunks: list[str] = []
    for match in re.finditer(rb"[\x20-\x7e]{4,}", raw):
        chunks.append(match.group(0).decode("ascii"))
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", raw):
        chunks.append(match.group(0).decode("utf-16-le", errors="ignore"))
    if decoded and ("\n" in decoded or "@" in decoded or "=" in decoded):
        return decoded
    if decoded:
        chunks.insert(0, decoded)
    return "\n".join(chunks)


def load_config(path: Path) -> PrivacyConfig:
    """Load and validate the small, checked-in privacy policy config."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError("privacy scan config must use schema_version 1")

    expected_baseline = raw.get("baseline_sha256")
    if expected_baseline != _BUILTIN_PRIVACY_BASELINE_SHA256:
        raise ValueError("privacy scan config does not attest the immutable built-in baseline")

    normalized = _normalise_policy_payload(raw)
    baseline = _normalise_policy_payload(_BUILTIN_PRIVACY_BASELINE)
    # Scan coverage may only grow, while exclusions and synthetic exceptions
    # may only shrink. This makes a policy file a tightening layer, never a
    # way to expand an allowlist or hide a private path.
    for key in ("source_roots", "artifact_runtime_roots", "text_suffixes", "forbidden_name_tokens", "forbidden_content_tokens"):
        if not set(baseline[key]).issubset(normalized[key]):
            raise ValueError(f"privacy scan config weakens the built-in baseline field {key!r}")
    for key in ("excluded_paths", "synthetic_name_values", "synthetic_email_domains", "synthetic_legal_markers"):
        if not set(normalized[key]).issubset(baseline[key]):
            raise ValueError(f"privacy scan config expands the built-in allowlist field {key!r}")

    def _strings(key: str) -> tuple[str, ...]:
        values = raw.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"privacy scan config field {key!r} must be a string list")
        return tuple(item for item in values if item)

    source_roots = _strings("source_roots")
    if not source_roots:
        raise ValueError("privacy scan config must name at least one source root")
    artifact_runtime_roots = tuple(
        item.rstrip("/").casefold() for item in _strings("artifact_runtime_roots")
    )
    if not artifact_runtime_roots:
        raise ValueError("privacy scan config must name artifact runtime roots")
    return PrivacyConfig(
        source_roots=source_roots,
        artifact_runtime_roots=artifact_runtime_roots,
        excluded_paths=frozenset(
            item.replace("\\", "/").casefold() for item in _strings("excluded_paths")
        ),
        synthetic_name_values=frozenset(
            item.casefold() for item in _strings("synthetic_name_values")
        ),
        synthetic_email_domains=frozenset(
            item.casefold().lstrip("@") for item in _strings("synthetic_email_domains")
        ),
        synthetic_legal_markers=tuple(
            item.casefold() for item in _strings("synthetic_legal_markers")
        ),
        text_suffixes=frozenset(
            item.casefold() if item.startswith(".") else "." + item.casefold()
            for item in _strings("text_suffixes")
        ),
        forbidden_name_tokens=frozenset(item.casefold() for item in _strings("forbidden_name_tokens")),
        forbidden_content_tokens=frozenset(item.casefold() for item in _strings("forbidden_content_tokens")),
        baseline_sha256=str(expected_baseline),
    )


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _line_is_synthetic(text: str, offset: int, config: PrivacyConfig) -> bool:
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end].casefold()
    return any(marker in line for marker in config.synthetic_legal_markers)


def _line_is_pattern_definition(text: str, offset: int) -> bool:
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end].casefold()
    previous_start = text.rfind("\n", 0, max(0, line_start - 1)) + 1
    previous = text[previous_start:line_start].casefold()
    # A real phone/contact field may appear on the same line as another
    # identifier assignment. Do not let the ``_phone =`` substring inside
    # ``contact_phone = ...`` classify the whole line as a regex definition.
    if re.search(r"\b(?:phone|mobile|telephone|tel|contact_phone)\b\s*[:=]", line, re.IGNORECASE):
        return False
    return (
        "re.compile(" in line or "pattern =" in line or "_phone =" in line
        or "re.compile(" in previous or "_phone =" in previous
    )


def _is_date_like(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", value.strip()))


def _looks_like_phone(value: str, *, contextual: bool = False) -> bool:
    """Require phone-like punctuation and exclude IPs, dates, and counters."""

    value = value.strip()
    if _is_date_like(value):
        return False
    if re.search(r"[A-Za-z]", value):
        return False
    digits = re.sub(r"\D", "", value)
    if not 9 <= len(digits) <= 15:
        return False
    if value.count(".") >= 2 and all(
        part.isdigit() and len(part) <= 3 for part in value.split(".")
    ):
        return False
    return contextual or bool(re.search(r"[+()\s-]", value))


def _is_identifier_number(text: str, start: int) -> bool:
    """Exclude the numeric tail of REQ/JOB identifiers, not nearby phones."""

    prefix = text[max(0, start - 24):start]
    return bool(re.search(r"(?:requisition|req|job(?:\s+id)?|id)\s*[-_:#]?\s*$", prefix, re.IGNORECASE))


def _scan_text(text: str, *, label: str, config: PrivacyConfig) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []

    for match in _NAME_ASSIGNMENT_PATTERN.finditer(text):
        value = match.group("value").strip()
        if value.casefold() not in config.synthetic_name_values:
            findings.append(
                PrivacyFinding(label, "name", _line_number(text, match.start()))
            )

    for match in _EMAIL_PATTERN.finditer(text):
        domain = match.group(0).rsplit("@", 1)[-1].casefold()
        if domain not in config.synthetic_email_domains and not any(
            domain.endswith("." + allowed) for allowed in config.synthetic_email_domains
        ):
            findings.append(
                PrivacyFinding(label, "email", _line_number(text, match.start()))
            )

    for pattern in (_PHONE_FIELD_PATTERN, _PHONE_FIELD_NUMBER_PATTERN):
        for match in pattern.finditer(text):
            if not _line_is_pattern_definition(text, match.start()) and _looks_like_phone(match.group("value"), contextual=True):
                findings.append(
                    PrivacyFinding(label, "phone", _line_number(text, match.start()))
                )

    for match in _PHONE_PATTERN.finditer(text):
        value = match.group(0).strip()
        if (
            not _line_is_pattern_definition(text, match.start())
            and not _is_identifier_number(text, match.start())
            and _looks_like_phone(value)
        ):
            findings.append(
                PrivacyFinding(label, "phone", _line_number(text, match.start()))
            )

    for match in _USER_PATH_PATTERN.finditer(text):
        findings.append(
            PrivacyFinding(label, "user_path", _line_number(text, match.start()))
        )

    for pattern in _LEGAL_PATTERNS:
        for match in pattern.finditer(text):
            if not _line_is_synthetic(text, match.start(), config):
                findings.append(
                    PrivacyFinding(label, "legal_status", _line_number(text, match.start()))
                )

    return sorted(
        set(findings),
        key=lambda finding: (finding.path.casefold(), finding.line, finding.category),
    )


def _iter_source_files(root: Path, config: PrivacyConfig) -> Iterable[Path]:
    for relative in config.source_roots:
        path = root / relative
        if path.is_file() and not path.is_symlink():
            yield path
        elif path.is_dir() and not path.is_symlink():
            for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
                if child.is_file() and not child.is_symlink():
                    yield child


def _read_text_if_configured(path: Path, config: PrivacyConfig) -> str | None:
    if path.suffix.casefold() in _BINARY_SKIP_SUFFIXES:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > 16 * 1024 * 1024:
        return None
    return _decode_payload(raw)


def _is_excluded(label: str, config: PrivacyConfig) -> bool:
    normalized = label.replace("\\", "/").casefold()
    return any(
        normalized == excluded
        or normalized.startswith(excluded + "/")
        or normalized.endswith("/" + excluded)
        or f"/{excluded}/" in "/" + normalized
        for excluded in config.excluded_paths
    )


def scan_source(root: Path, config: PrivacyConfig) -> list[PrivacyFinding]:
    """Scan configured production source roots in deterministic order."""

    root = root.expanduser().resolve()
    findings: list[PrivacyFinding] = []
    for path in sorted(
        _iter_source_files(root, config), key=lambda item: item.as_posix().casefold()
    ):
        relative = path.relative_to(root).as_posix()
        if _is_excluded(relative, config):
            continue
        text = _read_text_if_configured(path, config)
        if text is None:
            continue
        findings.extend(
            _scan_text(text, label=relative, config=config)
        )
    return sorted(
        set(findings),
        key=lambda finding: (finding.path.casefold(), finding.line, finding.category),
    )


def scan_artifact(path: Path, config: PrivacyConfig) -> list[PrivacyFinding]:
    """Scan runtime text members of a zip release or standalone text artifact.

    Release archives also contain development tests and evidence documents.
    The checked-in runtime-root allowlist keeps those synthetic fixtures out of
    the gate while still scanning every file that can ship in the executable
    payload (application, scripts, extension, and launcher code).
    """

    path = path.expanduser().resolve()
    findings: list[PrivacyFinding] = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist(), key=str.casefold):
                member = Path(name)
                if _is_excluded(name, config):
                    continue
                relative_name = name.split("/", 1)[-1]
                normalized_relative = relative_name.casefold()
                if name.endswith("/") or member.suffix.casefold() in _BINARY_SKIP_SUFFIXES:
                    continue
                text = _decode_payload(archive.read(name))
                findings.extend(_scan_text(text, label=f"{path.name}!{name}", config=config))
    else:
        text = _read_text_if_configured(path, config)
        if text is not None:
            findings.extend(_scan_text(text, label=path.name, config=config))
    return sorted(
        set(findings),
        key=lambda finding: (finding.path.casefold(), finding.line, finding.category),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("packaging/privacy_scan_config.json"),
    )
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_config(config_path.resolve())
    findings = scan_source(root, config)
    for artifact in args.artifact:
        findings.extend(scan_artifact(artifact, config))
    findings = sorted(
        set(findings),
        key=lambda finding: (finding.path.casefold(), finding.line, finding.category),
    )
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.category}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
