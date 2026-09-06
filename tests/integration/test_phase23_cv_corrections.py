from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import struct
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from scripts.phase23_cv_corrections import (
    NON_QUANT_MONOLITH_LINES,
    QUANT_MONOLITH_LINES,
    UnsupportedSource,
    apply_plan,
    build_plan,
    extract_docx,
    load_preflight_report,
    load_render_qa,
    main,
    save_preflight_report,
    save_render_qa,
    stage_plan,
    transform_docx,
    validate_graduation,
)


PWC_CURLY = "PwC’s"
OLD_MONOLITH = (
    "Founded Monolith Quant Research: backtesting and risk-managing systematic "
    "strategies across equities, rates, FX, commodities and crypto — skills "
    "directly transferable to markets."
)


def _docx_bytes(*paragraph_runs: tuple[str, ...]) -> bytes:
    paragraphs = []
    for runs in paragraph_runs:
        run_xml = "".join(
            f'<w:r><w:t xml:space="preserve">{escape(run)}</w:t></w:r>'
            for run in runs
        )
        paragraphs.append(
            '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/>'
            '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/>'
            f"</w:numPr></w:pPr>{run_xml}</w:p>"
        )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            "</Types>",
        )
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


def _valid_source(
    pwc_runs: tuple[str, ...],
    monolith_runs: tuple[str, ...],
    *extra_paragraphs: tuple[str, ...],
) -> bytes:
    return _docx_bytes(
        ("Bachelor of Science: Finance",),
        ("2025 – 2028",),
        pwc_runs,
        monolith_runs,
        *extra_paragraphs,
    )


def test_transform_reconstructs_split_runs_and_preserves_paragraph_count() -> None:
    source = _valid_source(
        (
            "Embedded within ",
            f"{PWC_CURLY} Risk Analytics division, gaining hands-on ",
            "exposure to quantitative risk frameworks.",
        ),
        (
            "Founded Monolith Quant Research: backtesting and risk-managing ",
            "systematic strategies across equities, rates, FX, commodities and crypto — ",
            "skills directly transferable to markets.",
        ),
        (
            "Ranked top 10 in the UK out of 2,200 competing teams — one of the strongest "
            "UK placements nationally",
        ),
    )

    result = transform_docx(
        source,
        is_quant=False,
        tags=frozenset({"grad-2028", "summer-cv"}),
        employer_names=("PwC",),
    )

    assert "Embedded within" not in result.text
    assert "hands-on" not in result.pwc_paragraph
    assert result.pwc_paragraph == (
        "Shadowed analysts in PwC’s Risk Analytics division, observing quantitative "
        "risk frameworks."
    )
    assert result.monolith_lines == NON_QUANT_MONOLITH_LINES
    assert result.output_paragraph_count == result.source_paragraph_count == 5
    assert result.fat_clauses_removed == 1
    assert "Ranked top 10 in the UK out of 2,200 competing teams." in result.text


@pytest.mark.parametrize(
    ("opening", "subject"),
    (
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "financial controls.",
        ),
        (
            "Embedded within PwC’s Risk Analytics division, developing fluency in ",
            "risk analytics.",
        ),
        (
            "Completed PwC’s Risk Analytics division work-experience placement, "
            "gaining exposure to ",
            "risk reporting.",
        ),
        (
            "Completed PwC's Risk Analytics division work-experience placement, "
            "gaining exposure to ",
            "risk reporting.",
        ),
    ),
)
def test_transform_supports_only_the_brief_authorized_pwc_openings(
    opening: str,
    subject: str,
) -> None:
    result = transform_docx(
        _valid_source((opening, subject), (OLD_MONOLITH,)),
        is_quant=False,
        tags=frozenset({"grad-2028"}),
        employer_names=("PwC",),
    )

    assert result.pwc_paragraph == (
        "Shadowed analysts in PwC’s Risk Analytics division, observing " + subject
    )


def test_transform_rejects_unapproved_pwc_wording_without_improvising() -> None:
    source = _valid_source(
        (
            "Embedded within PwC’s Risk Analytics division, gaining practical exposure "
            "to financial controls.",
        ),
        (OLD_MONOLITH,),
    )

    with pytest.raises(UnsupportedSource, match="unsupported_pwc_opening"):
        transform_docx(
            source,
            is_quant=False,
            tags=frozenset({"grad-2028"}),
            employer_names=("PwC",),
        )


def test_quant_copy_is_exact_and_failure_language_never_enters_non_quant() -> None:
    source = _valid_source(
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "financial controls.",
        ),
        (OLD_MONOLITH,),
    )

    quant = transform_docx(
        source,
        is_quant=True,
        tags=frozenset({"grad-2028", "quant"}),
        employer_names=("PwC",),
    )
    non_quant = transform_docx(
        source,
        is_quant=False,
        tags=frozenset({"grad-2028", "banking"}),
        employer_names=("PwC",),
    )

    assert quant.monolith_lines == QUANT_MONOLITH_LINES
    assert non_quant.monolith_lines == NON_QUANT_MONOLITH_LINES
    for phrase in ("kill record", "retired", "overfitting"):
        assert phrase not in non_quant.text.casefold()


def test_fat_trimming_preserves_facts_and_named_employer_tailoring() -> None:
    source = _valid_source(
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "financial controls.",
        ),
        (OLD_MONOLITH,),
        (
            "Completed simulation — competed through 6 rounds of live market simulation "
            "covering market-making, arbitrage, and options trading",
        ),
        ("Built analysis — aligned with Goldman’s rigorous analytical culture",),
        (
            "Prepared reporting — directly applicable to a capital markets placement "
            "at RBC",
        ),
        (
            "Led society events, demonstrating the initiative and analytical precision "
            "valued in fast-paced financial environments",
        ),
    )

    result = transform_docx(
        source,
        is_quant=False,
        tags=frozenset({"grad-2028"}),
        employer_names=("PwC", "Goldman Sachs"),
    )

    assert "competed through 6 rounds" in result.text
    assert "market-making, arbitrage, and options trading" in result.text
    assert "aligned with Goldman’s rigorous analytical culture" in result.text
    assert "directly applicable to a capital markets placement at RBC" in result.text
    assert "demonstrating the initiative" not in result.text
    assert result.fat_clauses_removed == 1
    assert result.employer_flattery_clauses == 2


def test_fat_trimming_removes_every_generic_evidence_free_lead_form() -> None:
    tails = (
        "sharpening the communication, stakeholder management and commercial awareness "
        "central to investment banking.",
        "developing leadership, teamwork, event management and commercial awareness.",
        "skills that translate directly to data-driven consulting and advisory placement work",
        "demonstrating the research rigour and data interpretation skills central to "
        "economics research and policy advisory roles",
        "combining analytical rigour with the communication and teamwork valued in "
        "data-driven finance.",
    )
    source = _valid_source(
        (
            "Embedded within PwC’s Risk Analytics division, gaining exposure to ",
            "financial controls.",
        ),
        (OLD_MONOLITH,),
        *((f"Evidence {index} — {tail}",) for index, tail in enumerate(tails)),
    )

    result = transform_docx(
        source,
        is_quant=False,
        tags=frozenset({"grad-2028"}),
        employer_names=("PwC",),
    )

    assert result.fat_clauses_removed == len(tails)
    for tail in tails:
        assert tail not in result.text


@pytest.mark.parametrize(
    ("text", "tags", "expected"),
    (
        (
            "Bachelor of Science: Finance\n2025 – 2028",
            frozenset({"grad-2028"}),
            "grad-2028",
        ),
        (
            "Bachelor of Science: Finance with Year in Industry\n2025 – 2029",
            frozenset({"grad-2029"}),
            "grad-2029",
        ),
    ),
)
def test_validate_graduation_accepts_only_the_coupled_degree_and_year(
    text: str,
    tags: frozenset[str],
    expected: str,
) -> None:
    assert validate_graduation(text, tags) == expected


@pytest.mark.parametrize(
    ("text", "tags"),
    (
        (
            "Bachelor of Science: Finance with Year in Industry\n2025 – 2028",
            frozenset({"grad-2028"}),
        ),
        (
            "Bachelor of Science: Finance\n2025 – 2029",
            frozenset({"grad-2029"}),
        ),
        (
            "Bachelor of Science: Finance\n2025 – 2028\nGraduating 2029",
            frozenset({"grad-2028"}),
        ),
    ),
)
def test_validate_graduation_rejects_every_mismatch(
    text: str,
    tags: frozenset[str],
) -> None:
    with pytest.raises(UnsupportedSource, match="graduation_coupling"):
        validate_graduation(text, tags)


def test_extract_docx_rejects_an_invalid_package() -> None:
    with pytest.raises(UnsupportedSource, match="invalid_docx"):
        extract_docx(b"not a docx")


def _migration_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE documents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                approved INTEGER NOT NULL,
                tags_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX ix_documents_kind ON documents(kind);
            CREATE TABLE opportunities (employer TEXT NOT NULL);
            CREATE TABLE sentinel (value TEXT NOT NULL);
            INSERT INTO opportunities(employer) VALUES ('PwC'), ('Goldman Sachs');
            INSERT INTO sentinel(value) VALUES ('unchanged');
            """
        )
        connection.commit()
    finally:
        connection.close()


def _insert_source(
    db_path: Path,
    documents_dir: Path,
    *,
    document_id: str,
    filename: str,
    content: bytes,
    tags: tuple[str, ...],
) -> Path:
    target = documents_dir / filename
    target.write_bytes(content)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO documents "
            "(id,name,kind,path,sha256,approved,tags_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                document_id,
                filename,
                "cv",
                str(target.resolve()),
                hashlib.sha256(content).hexdigest(),
                1,
                json.dumps(list(tags)),
                datetime(2026, 8, 27, 12).isoformat(" "),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return target


def _migration_source(*, supported: bool = True) -> bytes:
    pwc = (
        "Embedded within PwC’s Risk Analytics division, gaining exposure to "
        if supported
        else "Embedded within PwC’s Risk Analytics division, gaining practical exposure to "
    )
    return _valid_source((pwc, "financial controls."), (OLD_MONOLITH,))


def _table_rows(db_path: Path, table: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return list(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
    finally:
        connection.close()


def test_build_plan_is_read_only_and_reports_unsupported_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    _insert_source(
        db_path,
        documents_dir,
        document_id="supported",
        filename="supported.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv", "banking"),
    )
    _insert_source(
        db_path,
        documents_dir,
        document_id="unsupported",
        filename="unsupported.docx",
        content=_migration_source(supported=False),
        tags=("grad-2028", "summer-cv", "banking"),
    )
    db_before = db_path.read_bytes()
    files_before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in documents_dir.iterdir()
    }

    plan = build_plan(db_path, documents_dir)

    assert len(plan.eligible) == 1
    assert plan.eligible[0].source_id == "supported"
    assert [(item.source_id, item.reason) for item in plan.skipped] == [
        ("unsupported", "unsupported_pwc_opening")
    ]
    assert db_path.read_bytes() == db_before
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in documents_dir.iterdir()
    } == files_before


def test_stage_and_apply_create_new_file_and_row_without_deleting_or_cross_table_writes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    stage_dir = tmp_path / "stage"
    documents_dir.mkdir()
    _migration_database(db_path)
    source_path = _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv", "banking"),
    )
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    sentinel_before = _table_rows(db_path, "sentinel")

    staged = stage_plan(build_plan(db_path, documents_dir), stage_dir)
    report = apply_plan(staged, lock_retries=3, retry_delay=0.01)

    rows = _table_rows(db_path, "documents")
    assert report.applied == 1
    assert report.already_applied == 0
    assert len(rows) == 2
    source = next(row for row in rows if row[0] == "source-id")
    derived = next(row for row in rows if row[0] != "source-id")
    assert source[5] == 0
    assert derived[5] == 1
    assert json.loads(derived[6]) == ["grad-2028", "summer-cv", "banking"]
    assert Path(derived[3]).is_file()
    assert hashlib.sha256(Path(derived[3]).read_bytes()).hexdigest() == derived[4]
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    assert _table_rows(db_path, "sentinel") == sentinel_before
    assert {row[0] for row in rows} >= {"source-id"}

    repeated = apply_plan(staged, lock_retries=3, retry_delay=0.01)
    assert repeated.applied == 0
    assert repeated.already_applied == 1
    assert len(_table_rows(db_path, "documents")) == 2


def test_apply_aborts_before_writing_when_source_drifted(tmp_path: Path) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    source_path = _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv"),
    )
    staged = stage_plan(build_plan(db_path, documents_dir), tmp_path / "stage")
    source_path.write_bytes(source_path.read_bytes() + b"drift")

    with pytest.raises(UnsupportedSource, match="source_file_drift"):
        apply_plan(staged, lock_retries=1, retry_delay=0)

    rows = _table_rows(db_path, "documents")
    assert len(rows) == 1
    assert rows[0][5] == 1


def test_apply_retries_a_real_sqlite_write_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv"),
    )
    staged = stage_plan(build_plan(db_path, documents_dir), tmp_path / "stage")
    lock = sqlite3.connect(db_path, timeout=0.01, check_same_thread=False)
    lock.execute("BEGIN IMMEDIATE")

    def release_lock() -> None:
        time.sleep(0.08)
        lock.rollback()
        lock.close()

    releaser = threading.Thread(target=release_lock)
    releaser.start()
    try:
        report = apply_plan(staged, lock_retries=10, retry_delay=0.02)
    finally:
        releaser.join(timeout=2)

    assert report.applied == 1


def test_preflight_report_round_trip_revalidates_staged_bytes(tmp_path: Path) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv", "banking"),
    )
    staged = stage_plan(build_plan(db_path, documents_dir), tmp_path / "stage")
    report_path = tmp_path / "preflight.json"

    save_preflight_report(staged, report_path)
    loaded = load_preflight_report(report_path)

    assert loaded.db_path == staged.db_path
    assert loaded.documents_dir == staged.documents_dir
    assert loaded.skipped == staged.skipped
    assert len(loaded.eligible) == 1
    assert loaded.eligible[0].output_sha256 == staged.eligible[0].output_sha256
    assert loaded.eligible[0].derived_content == staged.eligible[0].derived_content

    Path(loaded.eligible[0].staged_path or "").write_bytes(b"tampered")
    with pytest.raises(UnsupportedSource, match="staged_file_hash_mismatch"):
        load_preflight_report(report_path)


def test_preflight_report_detects_json_tampering(tmp_path: Path) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv"),
    )
    staged = stage_plan(build_plan(db_path, documents_dir), tmp_path / "stage")
    report_path = tmp_path / "preflight.json"
    save_preflight_report(staged, report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["payload"]["source_document_count"] = 999
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(UnsupportedSource, match="preflight_report_hash_mismatch"):
        load_preflight_report(report_path)


def _png_header(width: int = 1275, height: int = 1650) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def test_render_qa_requires_one_valid_page_for_every_output(tmp_path: Path) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv"),
    )
    plan = stage_plan(build_plan(db_path, documents_dir), tmp_path / "stage")
    item = plan.eligible[0]
    render_dir = tmp_path / "renders" / item.output_sha256
    render_dir.mkdir(parents=True)
    (render_dir / "page-1.png").write_bytes(_png_header())
    qa_path = tmp_path / "render-qa.json"

    save_render_qa(
        plan,
        tmp_path / "renders",
        qa_path,
        visual_inspected=True,
    )
    load_render_qa(qa_path, plan)

    (render_dir / "page-2.png").write_bytes(_png_header())
    with pytest.raises(UnsupportedSource, match="render_page_count"):
        save_render_qa(
            plan,
            tmp_path / "renders",
            tmp_path / "second-qa.json",
            visual_inspected=True,
        )


def test_preflight_cli_stages_and_writes_a_reloadable_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "argus.db"
    documents_dir = tmp_path / "documents"
    documents_dir.mkdir()
    _migration_database(db_path)
    _insert_source(
        db_path,
        documents_dir,
        document_id="source-id",
        filename="source.docx",
        content=_migration_source(),
        tags=("grad-2028", "summer-cv"),
    )
    report_path = tmp_path / "preflight.json"

    exit_code = main(
        [
            "--preflight",
            "--db",
            str(db_path),
            "--documents-dir",
            str(documents_dir),
            "--stage-dir",
            str(tmp_path / "stage"),
            "--report-json",
            str(report_path),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert summary["eligible"] == 1
    assert summary["skipped"] == 0
    assert len(load_preflight_report(report_path).eligible) == 1
