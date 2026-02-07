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
    NSButtonTypeMomentaryPushIn,
    NSButton,
    NSColor,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSScreen,
    NSScrollView,
    NSTextAlignmentLeft,
    NSTextField,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject
from icalevents.icalevents import events as ical_events

LOCAL_TZ = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
ZOOM_URL_RE = re.compile(
    r"(https?://[^\s<>\"]+|(?:[a-z0-9.-]+\.)?zoom\.us/[^\s<>\"]+)",
    re.IGNORECASE,
)


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

    if days:
        day_word = "day" if days == 1 else "days"
        if hours:
            hour_word = "hr" if hours == 1 else "hrs"
            return f"{days} {day_word} {hours} {hour_word}"
        return f"{days} {day_word}"
    if hours:
        hour_word = "hr" if hours == 1 else "hrs"
        if minutes:
            min_word = "min" if minutes == 1 else "mins"
            return f"{hours} {hour_word} {minutes} {min_word}"
        return f"{hours} {hour_word}"
    min_word = "min" if minutes == 1 else "mins"
    return f"{minutes} {min_word}"

def format_time(dt: datetime) -> str:
    return dt.strftime("%I:%M%p").lstrip("0").lower()

def format_relative(now: datetime, start: datetime, end: datetime) -> str:
    if start <= now <= end:
        return "current"
    if start.date() != now.date():
        return ""
    if now < start:
        return f"in {format_duration(start - now)}"
    return f"ended {format_duration(now - end)} ago"

def format_when(start: datetime, end: datetime, now: datetime) -> str:
    if start.date() == now.date():
        return f"{format_time(start)}-{format_time(end)}"
    if start.date() == end.date():
        return f"{start:%a, %b %d} {format_time(start)}-{format_time(end)}"
    return f"{start:%a, %b %d} {format_time(start)} - {end:%a, %b %d} {format_time(end)}"

def extract_zoom_link(
    location: str | None,
    title: str | None,
    description: str | None,
) -> str | None:
    for text in (location or "", title or "", description or ""):
        for match in ZOOM_URL_RE.findall(text):
            candidate = match.rstrip(").,;")
            if "://" not in candidate:
                candidate = f"https://{candidate}"
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
    zoom_link = extract_zoom_link(event.location, event.summary, event.description)

    return DisplayEvent(
        title=title,
        description=description,
        when_text=format_when(start, end, now),
        relative_text=format_relative(now, start, end),
        zoom_link=zoom_link,
    )

def make_label(
    text: str,
    frame,
    *,
    font_size: float,
    bold: bool = False,
    color=None,
    wraps: bool = False,
    selectable: bool = False,
):
    label = NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setEditable_(False)
    label.setSelectable_(selectable)
    label.setBezeled_(False)
    label.setBordered_(False)
    label.setDrawsBackground_(False)
    label.setAlignment_(NSTextAlignmentLeft)
    label.setFont_(
        NSFont.boldSystemFontOfSize_(font_size)
        if bold
        else NSFont.systemFontOfSize_(font_size)
    )
    if color is not None:
        label.setTextColor_(color)
    if wraps:
        label.cell().setWraps_(True)
        label.setLineBreakMode_(NSLineBreakByWordWrapping)
    else:
        label.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return label

def event_meta_line(event: DisplayEvent) -> str:
    if event.relative_text:
        return f"{event.when_text} ({event.relative_text})"
    return event.when_text

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

    screen = NSScreen.mainScreen()
    visible = screen.visibleFrame() if screen is not None else NSMakeRect(0.0, 0.0, 1440.0, 900.0)
    window_width = 255.0
    window_height = max(720.0, visible.size.height - 36.0)
    window_x = visible.origin.x + visible.size.width - window_width - 12.0
    window_y = visible.origin.y + 18.0

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(window_x, window_y, window_width, window_height),
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable,
        NSBackingStoreBuffered,
        False,
    )
    window.setTitle_("Meeting Events")
    window.setReleasedWhenClosed_(False)
    window.setDelegate_(controller)

    content = window.contentView()
    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.09, 0.11, 1.0).CGColor()
    )

    scroll = NSScrollView.alloc().initWithFrame_(content.bounds())
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    scroll.setHasVerticalScroller_(True)
    scroll.setDrawsBackground_(False)

    row_height = 240.0
    gap = 10.0
    padding = 10.0
    doc_width = 239.0
    doc_height = max(window_height - 30.0, (row_height + gap) * len(events) + padding * 2)
    document = NSView.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, doc_width, doc_height))
    document.setAutoresizingMask_(NSViewWidthSizable)
    document.setWantsLayer_(True)
    document.layer().setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.09, 0.11, 1.0).CGColor()
    )
    row_width = doc_width - (padding * 2)

    for idx, event in enumerate(events):
        y = doc_height - padding - row_height - idx * (row_height + gap)

        card = NSView.alloc().initWithFrame_(NSMakeRect(padding, y, row_width, row_height))
        card.setWantsLayer_(True)
        layer = card.layer()
        layer.setCornerRadius_(14.0)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.28, 0.33, 0.40, 1.0).CGColor()
        )
        layer.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.15, 0.18, 1.0).CGColor()
        )
        layer.setShadowColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.0, 0.0, 0.60).CGColor()
        )
        layer.setShadowOpacity_(0.45)
        layer.setShadowRadius_(5.0)
        layer.setShadowOffset_((0.0, -1.0))

        inner = 11.0
        content_width = row_width - (inner * 2)

        title = make_label(
            event.title,
            NSMakeRect(inner, row_height - 35.0, content_width, 22.0),
            font_size=14.0,
            bold=True,
            color=NSColor.colorWithCalibratedRed_green_blue_alpha_(0.93, 0.95, 0.98, 1.0),
        )
        card.addSubview_(title)

        meta = make_label(
            event_meta_line(event),
            NSMakeRect(inner, row_height - 58.0, content_width, 20.0),
            font_size=11.5,
            color=NSColor.colorWithCalibratedRed_green_blue_alpha_(0.62, 0.78, 0.97, 1.0),
        )
        card.addSubview_(meta)

        description_text = event.description
        if len(description_text) > 330:
            description_text = f"{description_text[:327]}..."
        description = make_label(
            description_text,
            NSMakeRect(inner, 58.0, content_width, 92.0),
            font_size=12.0,
            color=NSColor.colorWithCalibratedRed_green_blue_alpha_(0.78, 0.82, 0.88, 1.0),
            wraps=True,
        )
        card.addSubview_(description)

        zoom_line = f"Zoom: {event.zoom_link}" if event.zoom_link else "Zoom: (none found)"
        zoom = make_label(
            zoom_line,
            NSMakeRect(inner, 20.0, content_width, 30.0),
            font_size=11.5,
            color=NSColor.colorWithCalibratedRed_green_blue_alpha_(0.52, 0.85, 0.68, 1.0),
            wraps=True,
        )
        card.addSubview_(zoom)

        click_overlay = NSButton.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, row_width, row_height))
        click_overlay.setButtonType_(NSButtonTypeMomentaryPushIn)
        click_overlay.setBordered_(False)
        click_overlay.setTitle_("")
        click_overlay.setTag_(idx)
        click_overlay.setTarget_(controller)
        click_overlay.setAction_("openEvent:")
        card.addSubview_(click_overlay)

        document.addSubview_(card)

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
