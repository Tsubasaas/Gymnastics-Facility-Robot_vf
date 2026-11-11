"""UI/report preview without cameras, AI requests or robot controls.

Run: python preview_console.py (http://[::1]:8081/)
"""
import argparse
import json
import socket
import time
import vision_rules
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from report_data import load_report, evidence_path

ROOT = Path(__file__).resolve().parent
STARTED_AT = time.time()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        try:
            if url.path == "/api/state":
                state = {"preview_only": True, "now": time.time(), "clock_active": False,
                         "camera_ok": False, "judging": False, "section": None, "beat": None,
                         "people": [], "people_age": None, "alert": [], "last_judge": {},
                         "events": [{"t": STARTED_AT, "kind": "info", "code": "preview_ready",
                                     "text": "Preview server ready. Camera and robot controls are not connected."}],
                         "speech_events": [], "affect_log": [], "affect_stats": {}, "affects": {},
                         "names": list(vision_rules.NAMES), "nicknames": dict(vision_rules.NICKNAMES),
                         "added_at": dict(vision_rules.ADDED_AT), "max_participants": vision_rules.MAX_PARTICIPANTS,
                         "models": {"judge": "—", "preview": "—"}, "actions_ja": {},
                         "robot": {"running": False}, "sections": []}
                return self.respond(200, json.dumps(state, ensure_ascii=False).encode(), "application/json")
            if url.path in ("/", "/report"):
                name = "vision_ui.html" if url.path == "/" else "vision_report.html"
                return self.respond(200, (ROOT / name).read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/report":
                data = load_report(ROOT / "records", parse_qs(url.query).get("id", [None])[0])
                return self.respond(200 if data else 404,
                                    json.dumps(data or {"error": "No records"}, ensure_ascii=False).encode(),
                                    "application/json; charset=utf-8")
            if url.path.startswith("/evidence/"):
                return self.respond(200, evidence_path(ROOT / "records", url.path[10:]).read_bytes(), "image/jpeg")
        except (OSError, ValueError, KeyError, TypeError):
            return self.respond(400, b'{"error":"Record unavailable"}', "application/json")
        self.respond(404, b'{"error":"Preview only"}', "application/json")

    def respond(self, status, body, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    class LocalServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6
        daemon_threads = True
    LocalServer(("::1", args.port), Handler).serve_forever()
