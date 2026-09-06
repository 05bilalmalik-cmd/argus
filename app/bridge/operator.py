"""Explicit local operator commands. Never import this module into the MCP facade."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from uuid import UUID


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not print caller input or underlying private configuration errors.
        raise ValueError('INVALID_REQUEST')


def _canonical_id(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _open_bridge(data_dir: Path, endpoint: str | None = None):
    if not data_dir.is_absolute() or not data_dir.is_dir() or data_dir.is_symlink():
        raise ValueError('INVALID_REQUEST')
    from app.config import Settings
    from app.db import Database
    from app.security.crypto import CryptoBox
    from app.services.preparation_bridge import PreparationBridge

    values = {'ARGUS_DATA_DIR': str(data_dir.resolve(strict=True)),
              'ARGUS_AUTOMATION_MODE': 'REVIEW_ONLY',
              'ARGUS_ENABLE_LIVE_SUBMIT': 'false', 'ARGUS_ENABLE_TRACKR_LIVE': 'false',
              'ARGUS_SWEEP_INTERVAL_HOURS': '0'}
    if endpoint is not None:
        match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})', endpoint)
        if match is None or not 1 <= int(match[1]) <= 65535:
            raise ValueError('INVALID_REQUEST')
        values['ARGUS_PORT'] = match[1]
    settings = Settings.load(values)
    if (not (settings.data_dir / 'argus.db').is_file()
            or not settings.secret_key_path.is_file()):
        raise ValueError('NOT_READY')
    database = Database(settings)
    try:
        crypto = CryptoBox.from_path(settings.secret_key_path)
        def no_execution(*args):
            raise RuntimeError('NOT_READY')
        service = PreparationBridge(database, settings, crypto, None, no_execution, enabled=True)
        # No start(): an operator opening the store is not a runtime restart.
        return service, database
    except Exception:
        database.engine.dispose()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description='Local-only preparation authority for an existing ARGUS data root.')
    parser.add_argument('--data-dir', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    provision = commands.add_parser('provision')
    provision.add_argument('--endpoint', required=True)
    provision.add_argument('--credential-file', required=True, type=Path)
    provision.add_argument('--ttl-seconds', type=int, default=300)
    approve = commands.add_parser('approve')
    approve.add_argument('--application-id', required=True)
    approve.add_argument('--confirm-application-id', required=True)
    approve.add_argument('--ttl-seconds', type=int, default=300)
    revoke = commands.add_parser('revoke')
    revoke.add_argument('--grant-id', required=True)
    clear = commands.add_parser('clear-pause')
    clear.add_argument('--confirm-clear-pause', required=True, action='store_true')
    database = None
    try:
        args = parser.parse_args(argv)
        if not args.data_dir.is_absolute() or not args.data_dir.is_dir():
            raise ValueError('INVALID_REQUEST')
        if args.command == 'approve' and (
            not _canonical_id(args.application_id)
            or args.application_id != args.confirm_application_id
        ):
            raise ValueError('INVALID_REQUEST')
        if args.command == 'revoke' and not _canonical_id(args.grant_id):
            raise ValueError('INVALID_REQUEST')
        if args.command == 'provision' and not args.credential_file.is_absolute():
            raise ValueError('INVALID_REQUEST')
        service, database = _open_bridge(args.data_dir, getattr(args, 'endpoint', None))
        if args.command == 'provision':
            service.provision(args.credential_file, args.endpoint, args.ttl_seconds)
            result = {'status': 'PROVISIONED'}
        elif args.command == 'approve':
            grant_id = service.approve(args.application_id, args.confirm_application_id, args.ttl_seconds)
            if not _canonical_id(grant_id):
                raise ValueError('INTEGRITY')
            result = {'status': 'APPROVED', 'grant_id': grant_id}
        elif args.command == 'revoke':
            service.revoke(args.grant_id)
            result = {'status': 'REVOKED'}
        else:
            service.clear_pause()
            result = {'status': 'PAUSE_CLEARED'}
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'status': 'REFUSED'}))
        return 1
    finally:
        if database is not None:
            database.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
