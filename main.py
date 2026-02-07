#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "icalevents",
#     "flask",
#     "pyobjc",
# ]
# ///

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import objc
from flask import Flask, jsonify, render_template_string, request
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSScreen,
    NSScrollView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject, NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration
from icalevents.icalevents import events as ical_events

LOCAL_TZ = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
ZOOM_URL_RE = re.compile(
    r"(https?://[^\s<>\"]+|(?:[a-z0-9.-]+\.)?zoom\.us/[^\s<>\"]+)",
    re.IGNORECASE,
)
HOST = "127.0.0.1"
DEFAULT_SOURCE = "https://calendar.google.com/calendar/ical/9ea2d2e03cd799c6e7fe2e609af19480b1f1cc6fc2535b0c4ea700852522f8f8%40group.calendar.google.com/public/basic.ics"


@dataclass(slots=True)
class DisplayEvent:
    id: str
    title: str
    description: str
    start: datetime
    end: datetime
    zoom_link: str | None

    def as_json(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "start_iso": self.start.isoformat(),
            "end_iso": self.end.isoformat(),
            "zoom_link": self.zoom_link,
        }


def normalize_text(text: str | None, fallback: str) -> str:
    if not text:
        return fallback
    return " ".join(text.split())


def to_local(dt: datetime | None, default: datetime) -> datetime:
    if dt is None:
        return default
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def canonicalize_zoom_url(candidate: str) -> str | None:
    cleaned = candidate.strip().rstrip(").,;")
    if not cleaned:
        return None
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if not host.endswith("zoom.us"):
        return None
    return cleaned


def extract_zoom_link(
    location: str | None,
    title: str | None,
    description: str | None,
) -> str | None:
    # Location has precedence over title/description.
    for text in (location or "", title or "", description or ""):
        for match in ZOOM_URL_RE.findall(text):
            zoom_url = canonicalize_zoom_url(match)
            if zoom_url:
                return zoom_url
    return None


def load_events(source: str):
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "webcal"}:
        return ical_events(source, sort=True, fix_apple=True)

    if parsed.scheme == "file":
        local_path = Path(unquote(parsed.path))
        if local_path.exists():
            return ical_events(
                None,
                string_content=local_path.read_text(encoding="utf-8"),
                sort=True,
                fix_apple=True,
            )

    local_path = Path(source).expanduser()
    if local_path.exists():
        return ical_events(
            None,
            string_content=local_path.read_text(encoding="utf-8"),
            sort=True,
            fix_apple=True,
        )

    return ical_events(source, sort=True, fix_apple=True)


def to_display_event(event, now: datetime, index: int) -> DisplayEvent:
    start = to_local(event.start, now)
    end_guess = start + timedelta(minutes=30)
    end = to_local(event.end, end_guess)
    if end <= start:
        end = end_guess

    title = normalize_text(event.summary, "No title")
    description = normalize_text(event.description, "")
    zoom_link = extract_zoom_link(event.location, event.summary, event.description)
    event_id = f"{int(start.timestamp())}-{index}"

    return DisplayEvent(
        id=event_id,
        title=title,
        description=description,
        start=start,
        end=end,
        zoom_link=zoom_link,
    )


class EventProvider:
    def __init__(self, source: str):
        self.source = source

    def get_events(self) -> list[DisplayEvent]:
        now = datetime.now(tz=LOCAL_TZ)
        raw_events = load_events(self.source)
        return [to_display_event(event, now, i) for i, event in enumerate(raw_events)]


def exit_process_after_delay(delay_seconds: float = 0.05):
    def _exit_later():
        time.sleep(delay_seconds)
        os._exit(0)

    threading.Thread(target=_exit_later, daemon=True).start()


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Meeting Widget</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f2f4f8;
      --card: #ffffff;
      --border: #d5dde8;
      --text: #1f2530;
      --muted: #536071;
      --zoom: #0b6ad4;
      --shadow: 0 4px 14px rgba(16, 24, 40, 0.08);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1217;
        --card: #161b24;
        --border: #2b3342;
        --text: #e8edf7;
        --muted: #a5b0c2;
        --zoom: #80beff;
        --shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 13px;
      line-height: 1.35;
    }
    #events {
      width: 100%;
      padding: 10px;
      display: grid;
      gap: 10px;
    }
    .card {
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--card);
      box-shadow: var(--shadow);
      padding: 10px;
      display: grid;
      gap: 6px;
    }
    .card.has-zoom {
      cursor: pointer;
    }
    .card.has-zoom * {
      cursor: pointer;
    }
    .card.current {
      border-color: #2ea043;
      background: color-mix(in oklab, var(--card) 80%, #2ea043 20%);
    }
    .title {
      font-weight: 700;
      font-size: 14px;
      line-height: 1.25;
    }
    .time {
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
    }
    .description {
      color: var(--text);
      font-size: 12px;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 5;
      line-clamp: 5;
      -webkit-box-orient: vertical;
      overflow: hidden;
      word-break: break-word;
    }
    .zoom {
      display: block;
      color: var(--zoom);
      font-size: 11px;
      word-break: break-all;
      text-decoration: underline;
    }
    .empty, .error {
      margin: 10px;
      border: 1px dashed var(--border);
      border-radius: 12px;
      padding: 16px;
      text-align: center;
      color: var(--muted);
      background: var(--card);
    }
  </style>
</head>
<body>
  <div id="events"><div class="empty">Loading events…</div></div>
  <script>
    const state = { events: [], lastError: null };

    function sameDate(a, b) {
      return (
        a.getFullYear() === b.getFullYear() &&
        a.getMonth() === b.getMonth() &&
        a.getDate() === b.getDate()
      );
    }

    function formatTime(d) {
      return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
        .replace(" ", "")
        .toLowerCase();
    }

    function formatDuration(ms) {
      const totalMins = Math.max(0, Math.floor(ms / 60000));
      const hours = Math.floor(totalMins / 60);
      const mins = totalMins % 60;
      if (hours > 0 && mins > 0) return `${hours} ${hours === 1 ? "hr" : "hrs"} ${mins} ${mins === 1 ? "min" : "mins"}`;
      if (hours > 0) return `${hours} ${hours === 1 ? "hr" : "hrs"}`;
      return `${mins} ${mins === 1 ? "min" : "mins"}`;
    }

    function relativeText(now, start, end) {
      if (now >= start && now <= end) return "current";
      if (!sameDate(now, start)) return "";
      if (now < start) return `in ${formatDuration(start - now)}`;
      return `ended ${formatDuration(now - end)} ago`;
    }

    function whenText(now, start, end) {
      if (sameDate(now, start)) return `${formatTime(start)}-${formatTime(end)}`;
      const startDate = start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
      if (sameDate(start, end)) return `${startDate} ${formatTime(start)}-${formatTime(end)}`;
      const endDate = end.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
      return `${startDate} ${formatTime(start)} - ${endDate} ${formatTime(end)}`;
    }

    function cardFor(event) {
      const now = new Date();
      const start = new Date(event.start_iso);
      const end = new Date(event.end_iso);
      const rel = relativeText(now, start, end);
      const timeLine = rel ? `${whenText(now, start, end)} (${rel})` : whenText(now, start, end);

      const card = document.createElement("article");
      card.className = "card";
      if (rel === "current") {
        card.classList.add("current");
      }
      if (event.zoom_link) {
        card.classList.add("has-zoom");
        card.addEventListener("click", () => openZoom(event.zoom_link));
      }

      const title = document.createElement("div");
      title.className = "title";
      title.textContent = event.title || "No title";
      card.appendChild(title);

      const time = document.createElement("div");
      time.className = "time";
      time.textContent = timeLine;
      card.appendChild(time);

      if (event.description) {
        const desc = document.createElement("div");
        desc.className = "description";
        desc.textContent = event.description;
        card.appendChild(desc);
      }

      if (event.zoom_link) {
        const zoom = document.createElement("div");
        zoom.className = "zoom";
        zoom.textContent = event.zoom_link;
        card.appendChild(zoom);
      }

      return card;
    }

    async function openZoom(url) {
      try {
        const resp = await fetch("/open", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url }),
        });
        if (!resp.ok) {
          const result = await resp.json().catch(() => ({ error: "Failed to open link" }));
          alert(result.error || "Failed to open link");
        }
      } catch {
        // Process may exit immediately after successful open.
      }
    }

    function render() {
      const root = document.getElementById("events");
      root.innerHTML = "";

      if (state.lastError) {
        const error = document.createElement("div");
        error.className = "error";
        error.textContent = state.lastError;
        root.appendChild(error);
        return;
      }

      if (!state.events.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No events.";
        root.appendChild(empty);
        return;
      }

      for (const event of state.events) {
        root.appendChild(cardFor(event));
      }
    }

    async function refreshEvents() {
      try {
        const resp = await fetch("/api/events", { cache: "no-store" });
        if (!resp.ok) throw new Error("Could not load events.");
        state.events = await resp.json();
        state.lastError = null;
      } catch {
        state.events = [];
        state.lastError = "Could not load events.";
      }
      render();
    }

    refreshEvents();
    setInterval(refreshEvents, 5 * 60 * 1000);
    setInterval(render, 30 * 1000);
  </script>
</body>
</html>
"""


def create_app(provider: EventProvider) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(HTML_TEMPLATE)

    @app.get("/api/events")
    def api_events():
        try:
            events = [event.as_json() for event in provider.get_events()]
            return jsonify(events)
        except Exception as exc:
            return jsonify({"error": f"failed to load events: {exc}"}), 500

    @app.post("/open")
    def open_zoom():
        payload = request.get_json(silent=True) or {}
        requested_url = str(payload.get("url", "")).strip()
        zoom_url = canonicalize_zoom_url(requested_url)
        if not zoom_url:
            return jsonify({"ok": False, "error": "invalid zoom url"}), 400

        subprocess.run(["open", zoom_url], check=False)
        exit_process_after_delay()
        return jsonify({"ok": True})

    return app


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def wait_for_server(port: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def start_server(app: Flask, port: int) -> threading.Thread:
    thread = threading.Thread(
        target=app.run,
        kwargs={
            "host": HOST,
            "port": port,
            "debug": False,
            "use_reloader": False,
            "threaded": True,
        },
        daemon=True,
    )
    thread.start()
    return thread


class WebWidgetController(NSObject):
    def init(self):
        self = objc.super(WebWidgetController, self).init()
        if self is None:
            return None
        return self

    def windowWillClose_(self, _notification):
        NSApplication.sharedApplication().terminate_(None)


def show_web_widget(url: str):
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = WebWidgetController.alloc().init()

    screen = NSScreen.mainScreen()
    visible = screen.visibleFrame() if screen is not None else NSMakeRect(0.0, 0.0, 1440.0, 900.0)
    window_width = 280.0
    window_height = max(720.0, visible.size.height - 26.0)
    window_x = visible.origin.x + visible.size.width - window_width - 10.0
    window_y = visible.origin.y + 13.0

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(float(window_x), float(window_y), float(window_width), float(window_height)),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("Meeting Widget")
    window.setReleasedWhenClosed_(False)
    window.setDelegate_(controller)
    window.setLevel_(NSFloatingWindowLevel)

    content = window.contentView()
    scroll = NSScrollView.alloc().initWithFrame_(content.bounds())
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    scroll.setHasVerticalScroller_(False)
    scroll.setHasHorizontalScroller_(False)
    content.addSubview_(scroll)

    configuration = WKWebViewConfiguration.alloc().init()
    webview = WKWebView.alloc().initWithFrame_configuration_(scroll.bounds(), configuration)
    webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    request_obj = NSURLRequest.requestWithURL_(NSURL.URLWithString_(url))
    webview.loadRequest_(request_obj)
    scroll.setDocumentView_(webview)

    controller.window = window
    controller.webview = webview

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()


def main(source: str):
    provider = EventProvider(source)
    port = pick_free_port()
    app = create_app(provider)
    start_server(app, port)

    if not wait_for_server(port):
        raise RuntimeError("failed to start local Flask server")

    widget_url = f"http://{HOST}:{port}/"
    show_web_widget(widget_url)


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOURCE)
