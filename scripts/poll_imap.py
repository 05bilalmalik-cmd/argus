from __future__ import annotations

import imaplib
import os
import sys

from app.config import Settings
from app.db import Database
from app.services.email import MailService


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def main() -> int:
    settings = Settings.load()
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    host = required("ARGUS_IMAP_HOST")
    username = required("ARGUS_IMAP_USERNAME")
    password = required("ARGUS_IMAP_PASSWORD")
    folder = os.environ.get("ARGUS_IMAP_FOLDER", "INBOX")
    with imaplib.IMAP4_SSL(host) as client:
        client.login(username, password)
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open IMAP folder {folder!r}")
        status, data = client.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        message_ids = data[0].split()[-200:]
        ingested = 0
        with db.session_scope() as session:
            service = MailService(session)
            for message_id in message_ids:
                status, parts = client.fetch(message_id, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next(
                    (part[1] for part in parts if isinstance(part, tuple) and isinstance(part[1], bytes)),
                    None,
                )
                if raw:
                    service.ingest(raw)
                    ingested += 1
        print(f"ARGUS inspected {len(message_ids)} messages; ingested {ingested} payloads.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ARGUS IMAP error: {exc}", file=sys.stderr)
        raise SystemExit(1)
