#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "icalevents",
#     "httpx",
#     "pyobjc",
# ]
# ///

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import subprocess
import time

import httpx
from icalevents.icalevents import events as ical_events

LOCAL_TZ = ZoneInfo(time.tzname[0])

def notify(title: str, body: str = "(no body)"):
    script = f"""display notification "{body}" with title "{title}" """
    subprocess.run(["osascript", "-e", script], check=False)

def main(url: str):
    # ics = httpx.get(url).text
    # now = datetime.now(tz=LOCAL_TZ)
    # start = now - timedelta(minutes=5)
    # end = now + timedelta(minutes=5)
    # events = ical_events(None, string_content=ics, start=start, end=end)
    events = ical_events(url, sort=True)

    if len(events) == 0:
        print("no events")
    else:
        print(f"got {len(events)} events")
        for event in events:
            title = event.summary or "no summary"
            body = event.start.strftime("%Y-%m-%d %H:%M")
            location = event.location or "no location"
            description = event.description or "no description"
            print(f"{title=}, {body=}, {location=}, {description=}")

if __name__ == "__main__":
    import sys
    source = "https://calendar.google.com/calendar/ical/9ea2d2e03cd799c6e7fe2e609af19480b1f1cc6fc2535b0c4ea700852522f8f8%40group.calendar.google.com/public/basic.ics"
    main(source)
