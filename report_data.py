"""Read actual observation records; never substitute demonstration values."""
import json
import re
from pathlib import Path
from session_events import read_events

AFFECT_KEYS = ("愉悦", "兴趣", "满足", "愤怒", "焦虑", "悲伤")
POSITIVE = set(AFFECT_KEYS[:3])


def merge_events(raw, directory, people, sections, planned):
    events = read_events(raw.get("session_id"), directory)
    samples, affects, unknowns, utterances = [], {}, {}, {}
    section_map = {s["key"]: s for s in sections}
    seen_samples = set()
    for event in events:
        if event["event_type"] == "speech":
            utterances.setdefault(event["utterance_id"], []).append(event)
        elif event["event_type"] == "affect":
            if event["sample_id"] in seen_samples:
                continue
            seen_samples.add(event["sample_id"])
            samples.append(event)
            key = event.get("section") or "transition"
            sec = section_map.setdefault(key, {"key": key, "no": key.split("_")[0],
                                               "judged": False, "pos": 0, "neg": 0})
            for p in event.get("people", []):
                name = p.get("name")
                if not isinstance(name, str) or not re.fullmatch("[A-Z]", name):
                    continue
                people.setdefault(name, {"letter": name, "nickname": raw.get("nicknames", {}).get(name, ""),
                                         "completed": False})
                affect = p.get("affect")
                if affect not in AFFECT_KEYS:
                    unknowns[name] = unknowns.get(name, 0) + 1
                    continue
                counts = affects.setdefault(name, {a: 0 for a in AFFECT_KEYS})
                counts[affect] += 1
                sec["pos" if affect in POSITIVE else "neg"] += 1
    rows = []
    by_feedback = {p["feedback_id"]: p for p in planned if p.get("feedback_id")}
    matched = set()
    for uid, history in utterances.items():
        first, last = history[0], history[-1]
        fid = first.get("feedback_id")
        plan = by_feedback.get(fid)
        fields = ("observation_id", "name", "text", "section")
        consistent = all(all(h.get(f) == first.get(f) for f in (*fields, "feedback_id")) for h in history)
        match = plan and consistent and all(plan.get(f) == first.get(f) for f in fields)
        if match:
            matched.add(fid)
        rows.append({**(plan if match else {}), **{f: first.get(f) for f in fields},
                     "kind": first.get("kind", "narration"), "feedback_id": fid,
                     "utterance_id": uid, "t": last.get("at"), "status": last["status"],
                     "output": last.get("output"), "receipt_history": history,
                     "linked": bool(match) if fid else None,
                     "completion_confirmed": consistent and last["status"] == "completed"
                                             and last.get("output") == "local"})
    rows.extend(p for p in planned if not p.get("feedback_id") or p["feedback_id"] not in matched)
    ordered_sections = sorted(section_map.values(), key=lambda s: s["key"])
    return samples, affects, unknowns, rows, ordered_sections, events


def evidence_path(directory, name):
    if not re.fullmatch(r"[a-f0-9]{32}_\d+_\d+\.jpg", name):
        raise ValueError("Invalid evidence ID")
    return Path(directory) / "evidence" / name


def load_report(directory, record_id=None):
    directory = Path(directory)
    if record_id:
        if not re.fullmatch(r"taiso_[A-Za-z0-9_-]+\.json", record_id):
            raise ValueError("Invalid record ID")
        files = [directory / record_id]
    else:
        files = sorted(directory.glob("taiso_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    # A damaged latest record is an error, not permission to show an older session.
    raw = json.loads(files[0].read_text("utf-8"))
    people = {}
    sections, judgments, planned = [], {}, []
    for section in raw.get("sections", []):
        key = section["section"]
        sections.append({"key": key, "no": key.split("_")[0],
                         "judged": bool(section.get("results")), "pos": 0, "neg": 0})
        for result in section.get("results", []):
            name = result["name"]
            people[name] = {"letter": name, "nickname": raw.get("nicknames", {}).get(name, ""),
                            "completed": False}
            state = ("still" if result.get("moving") is False else
                     "ok" if result.get("ok") is True else
                     "issue" if result.get("issue") else "unknown")
            judgments.setdefault(name, {})[key] = {
                "r": state, "issue": result.get("issue"), "m": result.get("motion_score"),
                "observation_id": section.get("observation_id"),
                "captured_at": section.get("captured_at", []), "box": result.get("box")}
        for obs in section.get("planned_feedback", section.get("spoken", [])):
            name = obs.get("name", "")
            planned.append({"t": section.get("judged_at", "—"), "section": key,
                            "name": name, "kind": "planned",
                            "text": obs.get("text", f"{name}さん、{obs.get('advice', '')}"),
                            "status": "planned_not_confirmed",
                            "feedback_id": obs.get("feedback_id"),
                            "observation_id": section.get("observation_id")})
    samples, affects, unknowns, speech, sections, events = merge_events(raw, directory, people, sections, planned)
    return {"sample": False, "session_id": raw.get("session_id", files[0].stem),
            "source_file": files[0].name, "date": raw.get("date", "—"),
            "start": raw.get("started_at", "—"), "end": "—", "duration_s": None,
            "program": raw.get("program"), "speed_deg_s": "—", "robot": "—",
            "still_threshold": raw.get("still_threshold", 4.0),
            "participants": list(people.values()), "sections": sections,
            "affects": affects, "affect_samples": samples, "affect_unknowns": unknowns,
            "judgments": judgments, "spoken": speech, "events": events,
            "speech_completed": sum(1 for row in speech if row.get("completion_confirmed")),
            "follow_up": raw.get("small_movement", []),
            "models": raw.get("models", {}), "raw_record": raw,
            "coverage": {"completion": "not_verified",
                         "affect": "recorded" if samples else "not_recorded",
                         "speech": "receipts" if any(e["event_type"] == "speech" for e in events)
                         else "planned_not_confirmed"}}
