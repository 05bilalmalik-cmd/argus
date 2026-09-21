"""Run with: python -m app.tracker --data-dir PATH [--once | --open]."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='ARGUS public internship tracker (no auto-apply)')
    parser.add_argument('--data-dir', type=Path, required=True, help='Separate tracker data directory')
    parser.add_argument('--port', type=int, default=8791)
    parser.add_argument('--refresh-minutes', type=float, default=30)
    parser.add_argument('--once', action='store_true', help='Collect once, persist, print JSON and exit')
    parser.add_argument('--no-auto', action='store_true', help='Serve without startup/recurring collection')
    parser.add_argument('--discovery-only', action='store_true', help='Disable employer verification and alerts')
    parser.add_argument('--open', action='store_true', help='Open a new browser tab after healthy startup')
    args = parser.parse_args(argv)
    if not math.isfinite(args.refresh_minutes) or args.refresh_minutes < 1:
        parser.error('--refresh-minutes must be finite and at least 1')
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be between 1024 and 65535')
    from app.tracker.server import create_tracker_app

    app = create_tracker_app(args.data_dir, refresh_minutes=args.refresh_minutes,
                             auto_refresh=not args.no_auto, automated=not args.discovery_only)
    if args.once:
        ok = app.state.refresh.refresh_sync()
        state = app.state.refresh.snapshot()
        print(json.dumps({'refresh': state, 'summary': app.state.store.summary(),
                          'sources': app.state.store.list_sources()}, indent=2))
        sources = app.state.store.list_sources()
        complete = bool(sources) and all(source['status'] in {'ok', 'empty'} for source in sources)
        return 0 if ok and complete else 1
    if args.open:
        import threading
        import time
        import webbrowser
        import httpx

        def open_when_ready():
            url = f'http://127.0.0.1:{args.port}'
            for _ in range(30):
                try:
                    response = httpx.get(url + '/healthz', timeout=0.5, trust_env=False)
                    if response.json().get('service') == 'ARGUS Tracker':
                        webbrowser.open_new_tab(url)
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.5)
        threading.Thread(target=open_when_ready, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='info')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
