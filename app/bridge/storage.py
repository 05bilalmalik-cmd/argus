"""Companion authority store: never share the runner's SQLite write lock.

Only explicit enabled-service/local-operator actions initialize this file. Its
binding prevents accidentally reusing grants after moving the companion store.
All write transactions are short; none may enclose browser execution.
"""
from __future__ import annotations

import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path


class BridgeStorage:
    def __init__(self, database, settings):
        main = Path(database.engine.url.database).resolve(strict=True)
        expected = (Path(settings.data_dir) / 'argus.db').resolve(strict=True)
        if main != expected or not main.is_file():
            raise ValueError('NOT_READY')
        self.path = main.parent / 'preparation_bridge.sqlite3'
        self.binding = str(main)

    def initialize(self):
        with self.transaction() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS bridge_state (id INTEGER PRIMARY KEY CHECK(id=1), database_path TEXT NOT NULL, paused TEXT NOT NULL)')
            connection.execute("INSERT OR IGNORE INTO bridge_state VALUES (1, ?, '')", (self.binding,))
            if connection.execute('SELECT database_path FROM bridge_state WHERE id=1').fetchone()[0] != self.binding:
                raise ValueError('NOT_READY')
            connection.execute('CREATE TABLE IF NOT EXISTS bridge_grants (id TEXT PRIMARY KEY, application_id TEXT NOT NULL, fingerprint TEXT NOT NULL, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0)')
            connection.execute('CREATE TABLE IF NOT EXISTS bridge_runs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, grant_id TEXT NOT NULL UNIQUE, application_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, underlying_run_id TEXT, session_id TEXT)')
            connection.execute('CREATE TABLE IF NOT EXISTS bridge_credential (id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT NOT NULL, expires_at REAL NOT NULL)')

    def acquire_runtime(self):
        """OS-held lock, released on process death, never held by operator opens."""
        handle = open(self.path.with_suffix('.runtime.lock'), 'a+b')
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('BUSY') from None
        return handle

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA busy_timeout=5000')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
