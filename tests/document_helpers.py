from __future__ import annotations

import io
import zipfile


def cv_docx_bytes(year: int, marker: str = "Synthetic CV") -> bytes:
    """Return a minimal DOCX fixture with one unambiguous education range."""

    if year not in {2028, 2029}:
        raise ValueError("synthetic CV year must be 2028 or 2029")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{marker} Education 2025 – {year}</w:t>"
            "</w:r></w:p></w:body>"
            "</w:document>",
        )
    return buffer.getvalue()
