from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .sessionhub import DEFAULT_PAGE_SIZE, PROFILE_NAMES, SessionHubBackend, SessionHubError


def handle_api_request(
    db_by_profile: dict[str, str | Path],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    backend = SessionHubBackend(db_by_profile)
    try:
        if method == "GET" and path == "/health":
            return 200, {
                "ok": True,
                "contract": "session-hub-local-mvp",
                "profiles": list(PROFILE_NAMES),
                "defaultPageSize": DEFAULT_PAGE_SIZE,
            }
        if method == "POST" and path == "/sessions":
            payload = body or {}
            return 200, backend.list_sessions(
                tab=payload.get("tab", "all"),
                query=payload.get("query", ""),
                profiles=payload.get("profiles"),
                date_from=payload.get("dateFrom"),
                date_to=payload.get("dateTo"),
                telegram_types=payload.get("telegramTypes"),
                page_size=payload.get("pageSize", DEFAULT_PAGE_SIZE),
                cursor=payload.get("cursor"),
            )
        if method == "POST" and path == "/revalidate":
            payload = body or {}
            return 200, backend.revalidate_owner(
                str(payload.get("ownerProfile", "")),
                str(payload.get("storedSessionId", "")),
            )
        return 404, {"ok": False, "error": "not_found"}
    except SessionHubError as exc:
        return 400, {"ok": False, "error": exc.code}


def render_static_preview(data: dict[str, Any]) -> str:
    cards = []
    for item in data.get("items", [])[:20]:
        title = html.escape(str(item.get("title", "Untitled")))
        owner = html.escape(str(item.get("ownerProfile", "")))
        source = html.escape(str(item.get("source", "")))
        previews = "".join(
            f"<li>{html.escape(str(preview))}</li>"
            for preview in item.get("previews", [])[:3]
        )
        cards.append(
            "<article class='card'>"
            f"<h2>{title}</h2><p>{owner} · {source}</p><ul>{previews}</ul>"
            "</article>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Session Hub Preview</title>"
        "<style>"
        "body{font-family:system-ui;margin:0;background:#fff;color:#111}"
        "header{padding:16px;border-bottom:1px solid #ddd}"
        ".tabs{display:flex;gap:8px;padding:8px 16px;border-bottom:1px solid #ddd}"
        ".card{border:1px solid #ddd;border-radius:6px;margin:8px 16px;padding:12px}"
        "h1{font-size:18px}h2{font-size:14px;margin:0 0 4px}p,li{font-size:12px}"
        "</style></head><body>"
        "<header><h1>Session Hub</h1></header>"
        "<nav class='tabs'><button>Desktop</button><button>Telegram</button></nav>"
        f"<main>{''.join(cards) or '<p>No sessions</p>'}</main>"
        "</body></html>"
    )


class _Handler(BaseHTTPRequestHandler):
    db_by_profile: dict[str, str | Path] = {}
    preview_html: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path == "/" and self.preview_html is not None:
            self._html(200, self.preview_html)
            return
        status, payload = handle_api_request(self.db_by_profile, "GET", self.path, None)
        self._json(status, payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("content-length", "0") or 0)
        raw = self.rfile.read(min(length, 32768)) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "bad_json"})
            return
        status, payload = handle_api_request(self.db_by_profile, "POST", self.path, body)
        self._json(status, payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, document: str) -> None:
        body = document.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_loopback(
    db_by_profile: dict[str, str | Path],
    port: int = 8787,
    *,
    preview_html: str | None = None,
) -> ThreadingHTTPServer:
    class Handler(_Handler):
        pass

    Handler.db_by_profile = dict(db_by_profile)
    Handler.preview_html = preview_html
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server
