#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "icalevents",
#     "pyobjc",
# ]
# ///

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSButtonTypeMomentaryPushIn,
    NSLineBreakByWordWrapping,
    NSScrollView,
    NSTextAlignmentLeft,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject
from icalevents.icalevents import events as ical_events

LOCAL_TZ = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
ZOOM_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


@dataclass(slots=True)
class DisplayEvent:
    title: str
    description: str
    when_text: str
    relative_text: str
    zoom_link: str | None

def notify(title: str, body: str = "(no body)"):
    script = f"""display notification "{body}" with title "{title}" """
    subprocess.run(["osascript", "-e", script], check=False)

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

def format_duration(delta: timedelta) -> str:
    total_seconds = max(int(delta.total_seconds()), 0)
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts[:2])

def format_relative(now: datetime, start: datetime, end: datetime) -> str:
    if start <= now <= end:
        return "current"
    if now < start:
        return f"in {format_duration(start - now)}"
    return f"ended {format_duration(now - end)} ago"

def format_when(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%a, %b %d %I:%M %p} - {end:%I:%M %p}"
    return f"{start:%a, %b %d %I:%M %p} - {end:%a, %b %d %I:%M %p}"

def extract_zoom_link(title: str | None, description: str | None) -> str | None:
    for text in (title or "", description or ""):
        for match in ZOOM_URL_RE.findall(text):
            candidate = match.rstrip(").,;")
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            if host.endswith("zoom.us"):
                return candidate
    return None

def load_events(source: str):
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "webcal"}:
        return ical_events(source, sort=True)

    if parsed.scheme == "file":
        local_path = Path(unquote(parsed.path))
        if local_path.exists():
            return ical_events(
                None,
                string_content=local_path.read_text(encoding="utf-8"),
                sort=True,
            )

    local_path = Path(source).expanduser()
    if local_path.exists():
        return ical_events(
            None,
            string_content=local_path.read_text(encoding="utf-8"),
            sort=True,
        )

    return ical_events(source, sort=True)

def to_display_event(event, now: datetime) -> DisplayEvent:
    start = to_local(event.start, now)
    end_guess = start + timedelta(minutes=30)
    end = to_local(event.end, end_guess)
    if end <= start:
        end = end_guess

    title = normalize_text(event.summary, "No title")
    description = normalize_text(event.description, "(no description)")
    zoom_link = extract_zoom_link(event.summary, event.description)

    return DisplayEvent(
        title=title,
        description=description,
        when_text=format_when(start, end),
        relative_text=format_relative(now, start, end),
        zoom_link=zoom_link,
    )

def event_button_text(event: DisplayEvent) -> str:
    description = event.description
    if len(description) > 200:
        description = f"{description[:197]}..."
    link_line = event.zoom_link or "No Zoom link found"
    return (
        f"{event.title}\n"
        f"{event.when_text} ({event.relative_text})\n"
        f"{description}\n"
        f"{link_line}"
    )

class EventWindowController(NSObject):
    def initWithEvents_(self, events):
        self = objc.super(EventWindowController, self).init()
        if self is None:
            return None
        self.events = events
        return self

    @objc.IBAction
    def openEvent_(self, sender):
        idx = sender.tag()
        if idx < 0 or idx >= len(self.events):
            return
        event = self.events[idx]
        if event.zoom_link:
            subprocess.run(["open", event.zoom_link], check=False)
        else:
            notify("No Zoom link found", event.title)

    def windowWillClose_(self, _notification):
        NSApplication.sharedApplication().terminate_(None)

def show_events(events: list[DisplayEvent]):
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = EventWindowController.alloc().initWithEvents_(events)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(150.0, 150.0, 920.0, 660.0),
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("Meeting Events")
    window.setReleasedWhenClosed_(False)
    window.setDelegate_(controller)

    content = window.contentView()
    scroll = NSScrollView.alloc().initWithFrame_(content.bounds())
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    scroll.setHasVerticalScroller_(True)

    row_height = 120.0
    gap = 10.0
    padding = 12.0
    doc_width = 900.0
    doc_height = max(640.0, (row_height + gap) * len(events) + padding * 2)
    document = NSView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, doc_width, doc_height))

    for idx, event in enumerate(events):
        y = doc_height - padding - row_height - idx * (row_height + gap)
        button = NSButton.alloc().initWithFrame_(
            NSMakeRect(padding, y, doc_width - (padding * 2), row_height)
        )
        button.setButtonType_(NSButtonTypeMomentaryPushIn)
        button.setBezelStyle_(NSBezelStyleRounded)
        button.setAlignment_(NSTextAlignmentLeft)
        button.setTitle_(event_button_text(event))
        button.setTag_(idx)
        button.setTarget_(controller)
        button.setAction_("openEvent:")

        cell = button.cell()
        cell.setWraps_(True)
        cell.setLineBreakMode_(NSLineBreakByWordWrapping)

        document.addSubview_(button)

    scroll.setDocumentView_(document)
    content.addSubview_(scroll)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()

def main(url: str):
    raw_events = load_events(url)
    now = datetime.now(tz=LOCAL_TZ)
    events = [to_display_event(event, now) for event in raw_events]

    if len(events) == 0:
        print("no events")
        notify("No events", "No recurring or one-off events found in this window.")
    else:
        print(f"Loaded {len(events)} events")
        show_events(events)

if __name__ == "__main__":
    import sys
    source = "https://calendar.google.com/calendar/ical/9ea2d2e03cd799c6e7fe2e609af19480b1f1cc6fc2535b0c4ea700852522f8f8%40group.calendar.google.com/public/basic.ics"
    main(sys.argv[1] if len(sys.argv) > 1 else source)
