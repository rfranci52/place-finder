#!/usr/bin/env python3
"""Local API server for development: serves frontend/api/search.py at /api/search.

    uv run python scripts/dev_api.py --env-file PATH [--env-file PATH ...] [--port 8790]

The Vite dev server proxies /api here (see frontend/vite.config.ts), so the page
calls the same path in development and in production. The handler runs exactly as
deployed, behind the standard library's threading HTTP server. Settings come from
the process environment first, then from each --env-file in order; values are
parsed here, never handed to a shell or printed.
"""
import argparse
import importlib.util
import json
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

SEARCH_PY = Path(__file__).resolve().parent.parent / "frontend" / "api" / "search.py"
# The deployed function only answers its own site; locally that is the Vite dev server.
DEV_ORIGINS = "http://localhost:5174,http://127.0.0.1:5174"


def parse_env_file(path):
    """KEY=VALUE lines as a dict. Quotes around a value are removed."""
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env(paths):
    for path in paths:
        for key, value in parse_env_file(path).items():
            os.environ.setdefault(key, value)
    os.environ.setdefault("PLACES_ALLOWED_ORIGINS", DEV_ORIGINS)


def load_search():
    spec = importlib.util.spec_from_file_location("place_search", SEARCH_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_handler(search):
    class DevHandler(search.handler):
        """The deployed handler, answering only at the deployed path."""

        def do_POST(self):
            if self.path.split("?", 1)[0].rstrip("/") != "/api/search":
                body = json.dumps({"ok": False, "error": {"code": "invalid", "message": "Not found."}}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_POST()

    return DevHandler


def main():
    parser = argparse.ArgumentParser(description="Local API server for development")
    parser.add_argument("--env-file", action="append", default=[], help="KEY=VALUE file, may be repeated")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    load_env(args.env_file)
    # One line per upstream call is the point of the log, so flush each line even into a pipe.
    sys.stdout.reconfigure(line_buffering=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(load_search()))
    print(f"place-finder api on http://127.0.0.1:{args.port}/api/search", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
