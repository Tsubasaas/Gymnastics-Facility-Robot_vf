"""Independent atomic event files: camera and audio writers never overwrite each other."""
import json
import os
import re
import time
import uuid
from pathlib import Path

RECORDS_DIR = Path(__file__).resolve().parent / "records"


def valid_session(session_id):
    return isinstance(session_id, str) and re.fullmatch(r"[a-f0-9]{32}", session_id) is not None


def append_event(session_id, kind, payload, directory=None):
    if not valid_session(session_id):
        raise ValueError("Invalid session ID")
    event = {**payload, "session_id": session_id, "event_type": kind,
             "event_id": uuid.uuid4().hex, "recorded_at_ns": time.time_ns()}
    folder = Path(directory or RECORDS_DIR) / "events" / session_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (event["event_id"] + ".json")
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(event, ensure_ascii=False, allow_nan=False), "utf-8")
    os.replace(tmp, target)
    return event


def read_events(session_id, directory=None):
    if not valid_session(session_id):
        return []  # Older record formats have no event directory.
    folder = Path(directory or RECORDS_DIR) / "events" / session_id
    events = []
    for path in folder.glob("*.json"):
        event = json.loads(path.read_text("utf-8"))
        if event.get("session_id") != session_id:
            raise ValueError("Event session mismatch")
        events.append(event)
    return sorted(events, key=lambda e: (e["recorded_at_ns"], e["event_id"]))
