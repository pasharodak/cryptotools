#!/usr/bin/env python3
"""Local reverse-proxy + static UI (nginx stand-in for Windows).

Mirrors prod paths:
  /                -> site/custom-ui
  /api/pair-config/ -> 127.0.0.1:8090/
  /api/strategy/    -> 127.0.0.1:8081/api/v1/
  /api/grid/        -> 127.0.0.1:8082/api/v1/
  /api/finder/      -> 127.0.0.1:8080/api/v1/
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # site/
UI_DIR = ROOT / "custom-ui"

PROXY = {
    "/api/pair-config/": ("http://127.0.0.1:8090/", True),  # strip prefix
    "/api/strategy/": ("http://127.0.0.1:8081/api/v1/", True),
    "/api/grid/": ("http://127.0.0.1:8082/api/v1/", True),
    "/api/finder/": ("http://127.0.0.1:8080/api/v1/", True),
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_DELETE(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self) -> None:
        path = self.path.split("?", 1)[0]
        for prefix, (target, strip) in PROXY.items():
            if path.startswith(prefix) or path.rstrip("/") + "/" == prefix:
                self._proxy(prefix, target, strip)
                return
        self._static()

    def _proxy(self, prefix: str, target_base: str, strip: bool) -> None:
        raw = self.path
        if strip:
            rest = raw[len(prefix) :] if raw.startswith(prefix) else raw.lstrip("/")
            url = target_base.rstrip("/") + "/" + rest.lstrip("/")
        else:
            url = target_base.rstrip("/") + raw

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        path_only = raw.split("?", 1)[0]
        stream_sse = path_only.rstrip("/").endswith("/bots/events")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                if stream_sse or "text/event-stream" in (resp.headers.get("Content-Type") or ""):
                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                            continue
                        if k.lower() == "content-encoding":
                            continue
                        self.send_header(k, v)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    fp = getattr(resp, "fp", None) or resp
                    while True:
                        chunk = fp.read(256) if hasattr(fp, "read") else resp.read(256)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return
                data = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in HOP_BY_HOP:
                        continue
                    if k.lower() == "content-encoding":
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            msg = ('{"error":"upstream unavailable","detail":%s}' % json_dumps(str(e))).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _static(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("", "/"):
            rel = "index.html"
        else:
            rel = path.lstrip("/").replace("\\", "/")
        # prevent path escape
        candidate = (UI_DIR / rel).resolve()
        if not str(candidate).startswith(str(UI_DIR.resolve())):
            self.send_error(403)
            return
        if not candidate.is_file():
            # SPA fallback
            candidate = UI_DIR / "index.html"
        if not candidate.is_file():
            self.send_error(404)
            return
        data = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


def json_dumps(s: str) -> str:
    import json

    return json.dumps(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("LOCAL_SITE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LOCAL_SITE_PORT", "8443")))
    args = ap.parse_args()
    if not UI_DIR.is_dir():
        print(f"UI dir missing: {UI_DIR}", file=sys.stderr)
        return 1
    httpd = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"local site gateway http://{args.host}:{args.port}/  (ui={UI_DIR})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
