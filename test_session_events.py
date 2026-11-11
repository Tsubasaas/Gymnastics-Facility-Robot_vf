"""Offline audio lifecycle and expression/session join tests; no hardware or cloud calls."""
import importlib
import json
import queue
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import session_events
import report_data
from test_observation_records import coach
import demo_voice

with patch.dict("sys.modules", {"dashscope": types.ModuleType("dashscope"),
                                "llm_motion": types.SimpleNamespace(API_KEY="")}), patch("os.makedirs"):
    tts = importlib.import_module("tts_qwen")


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.job = {"id": "utterance1", "text": "同じ文章", "context": {"session_id": "a" * 32},
                    "on_event": self.events.append, "is_current": lambda: True}
        self.enabled = patch.object(tts, "_enabled", True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def process(self, sink=None, synth_error=None, play_error=None):
        with patch.object(tts, "synth", return_value="audio", side_effect=synth_error), \
             patch.object(tts, "_robot_sink", sink), \
             patch.object(tts, "_play_blocking", side_effect=play_error):
            tts._process(self.job)
        return [e["status"] for e in self.events]

    def test_local_completion_only_after_player_returns(self):
        self.assertEqual(self.process(), ["synthesizing", "playing", "completed"])
        self.assertEqual(self.events[-1]["output"], "local")

    def test_synthesis_failure_never_completes(self):
        self.assertEqual(self.process(synth_error=RuntimeError("network")), ["synthesizing", "failed"])
        self.assertEqual(self.events[-1]["stage"], "synthesis")

    def test_playback_error_never_completes(self):
        self.assertEqual(self.process(play_error=RuntimeError("device"))[-1], "failed")

    def test_robot_transport_is_not_completion(self):
        sink = Mock()
        sink.play_file.return_value = {"status": "robot_sent", "completion_confirmed": False}
        self.assertEqual(self.process(sink)[-1], "robot_sent")

    def test_fallback_is_preserved_before_local_completion(self):
        sink = Mock()
        sink.play_file.side_effect = RuntimeError("RPC")
        self.assertEqual(self.process(sink), ["synthesizing", "playing", "fallback", "playing", "completed"])

    def test_old_session_skips_before_synthesis(self):
        self.job["is_current"] = lambda: False
        self.assertEqual(self.process(), ["skipped"])

    def test_session_change_during_synthesis_skips_playback(self):
        self.job["is_current"] = Mock(side_effect=[True, False])
        self.assertEqual(self.process(), ["synthesizing", "skipped"])

    def test_muting_queued_jobs_has_receipts(self):
        q = queue.Queue()
        q.put(self.job)
        with patch.object(tts, "_q", q):
            tts.set_enabled(False)
        self.assertEqual(self.events[-1]["status"], "skipped")
        self.assertEqual(q.unfinished_tasks, 0)

    def test_same_text_has_distinct_utterance_ids(self):
        with patch.object(tts, "_q", queue.Queue()), patch.object(tts, "_worker", object()):
            first = tts.speak("same", on_event=self.events.append)
            second = tts.speak("same", on_event=self.events.append)
        self.assertNotEqual(first, second)
        self.assertEqual([e["status"] for e in self.events], ["queued", "queued"])

    def test_mci_nonzero_is_failure(self):
        mci = Mock(side_effect=[0, 7, 0])
        windll = types.SimpleNamespace(winmm=types.SimpleNamespace(mciSendStringW=mci))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            audio = Path(tmp) / "test.audio"
            audio.write_bytes(b"MP3!")
            with patch.object(tts.ctypes, "windll", windll, create=True):
                with self.assertRaisesRegex(RuntimeError, "playback failed"):
                    tts._play_blocking(str(audio))


class EventJoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.sid = "a" * 32

    def write_record(self, feedback=True):
        obsid = self.sid + ":1"
        raw = {"session_id": self.sid, "sections": [{"section": "3_udemawashi",
            "observation_id": obsid, "results": [{"name": "A", "ok": True}],
            "planned_feedback": [{"name": "A", "advice": "test", "feedback_id": obsid+":0"}] if feedback else []}]}
        (self.folder / f"taiso_{self.sid}.json").write_text(json.dumps(raw), "utf-8")
        return obsid

    def event(self, kind, data, sid=None):
        return session_events.append_event(sid or self.sid, kind, data, self.folder)

    def test_multiple_writers_do_not_lose_events(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.event("speech", {"utterance_id": str(i)}), range(20)))
        self.assertEqual(len(session_events.read_events(self.sid, self.folder)), 20)

    def test_expression_identity_unknown_and_capture_section(self):
        self.write_record(False)
        self.event("affect", {"sample_id": "s1", "section": "5_yokomage", "captured_at": 10,
            "people": [{"name": "A", "affect": "愉悦"}, {"name": "B", "affect": "?"},
                       {"name": "?", "affect": "悲伤"}], "status": "recognized"})
        self.event("affect", {"sample_id": "old", "people": [{"name": "C", "affect": "悲伤"}]}, "b"*32)
        r = report_data.load_report(self.folder)
        self.assertEqual(r["affects"]["A"]["愉悦"], 1)
        self.assertNotIn("B", r["affects"])
        self.assertEqual(r["affect_unknowns"]["B"], 1)
        self.assertNotIn("C", [p["letter"] for p in r["participants"]])
        self.assertEqual(next(s for s in r["sections"] if s["key"] == "5_yokomage")["pos"], 1)

    def test_feedback_receipts_join_by_id_not_text(self):
        oid = self.write_record()
        data = {"utterance_id": "u1", "feedback_id": oid+":0", "observation_id": oid,
                "section": "3_udemawashi", "name": "A", "text": "Aさん、test", "at": 12}
        self.event("speech", {**data, "status": "queued"})
        self.event("speech", {**data, "status": "completed", "output": "local"})
        r = report_data.load_report(self.folder)
        self.assertEqual(len(r["spoken"]), 1)
        self.assertTrue(r["spoken"][0]["linked"])
        self.assertEqual(r["speech_completed"], 1)
        self.assertEqual(len(r["spoken"][0]["receipt_history"]), 2)

    def test_mismatched_person_does_not_confirm_plan(self):
        oid = self.write_record()
        self.event("speech", {"utterance_id": "u1", "feedback_id": oid+":0", "observation_id": oid,
            "section": "3_udemawashi", "name": "B", "text": "Aさん、test", "status": "robot_sent"})
        r = report_data.load_report(self.folder)
        self.assertEqual(len(r["spoken"]), 2)
        self.assertFalse(r["spoken"][0]["linked"])
        self.assertEqual(r["speech_completed"], 0)

    def test_late_expression_is_written_to_capture_session(self):
        owner = coach.Director(self.sid)
        with patch.object(coach, "RECORDS_DIR", self.folder):
            coach.archive_affect_sample(owner, "3_udemawashi", 10,
                                        [{"name": "A", "affect": "愉悦"}])
        self.assertEqual(len(session_events.read_events(self.sid, self.folder)), 1)
        self.assertEqual(session_events.read_events("b"*32, self.folder), [])

    def test_voice_dispatch_to_receipt_to_report(self):
        oid = self.write_record()
        voice = demo_voice.VoiceCoach.__new__(demo_voice.VoiceCoach)
        voice._session_id, voice._key, voice._last_t = self.sid, "3_udemawashi", 5
        jobs = queue.Queue()
        with patch.object(session_events, "RECORDS_DIR", self.folder), \
             patch.object(demo_voice, "TTS_READY", True), \
             patch.object(demo_voice, "_tts_speak", tts.speak), \
             patch.object(tts, "_worker", object()), patch.object(tts, "_q", jobs), \
             patch.object(tts, "_enabled", True), patch.object(tts, "_robot_sink", None), \
             patch.object(tts, "synth", return_value="file"), patch.object(tts, "_play_blocking"):
            voice.coach_feedback([{"name": "A", "advice": "test", "feedback_id": oid+":0"}],
                                 observation_id=oid, section="3_udemawashi")
            self.assertEqual(report_data.load_report(self.folder)["spoken"][0]["status"], "queued")
            tts._process(jobs.get_nowait())
            result = report_data.load_report(self.folder)
            self.assertEqual(result["speech_completed"], 1)
            self.assertTrue(result["spoken"][0]["linked"])
            self.assertEqual([e["status"] for e in result["spoken"][0]["receipt_history"]],
                             ["queued", "synthesizing", "playing", "completed"])

    def test_subtitle_mode_never_claims_playback(self):
        self.write_record(False)
        with patch.object(session_events, "RECORDS_DIR", self.folder), \
             patch.object(demo_voice, "TTS_READY", False), \
             patch.object(demo_voice, "_TTS_WHY", "offline", create=True):
            demo_voice.say("test", context={"session_id": self.sid, "kind": "narration"})
        r = report_data.load_report(self.folder)
        self.assertEqual(r["spoken"][0]["status"], "subtitle_only")
        self.assertEqual(r["speech_completed"], 0)

    def test_unknown_affect_does_not_enter_positive_denominator(self):
        self.write_record(False)
        self.event("affect", {"sample_id": "s1", "section": "1_senobi", "captured_at": 10,
            "status": "recognized", "people": [{"name": "A", "affect": "?"}]})
        r = report_data.load_report(self.folder)
        self.assertEqual(r["affects"], {})
        self.assertEqual(r["affect_unknowns"], {"A": 1})

    def test_duplicate_expression_names_not_assigned(self):
        with patch.object(coach, "RECORDS_DIR", self.folder):
            e = coach.archive_affect_sample(coach.Director(self.sid), "3_udemawashi", 10,
                [{"name": "A", "affect": "愉悦"}, {"name": "A", "affect": "悲伤"}])
        self.assertEqual([p["name"] for p in e["people"]], ["?", "?"])


if __name__ == "__main__":
    unittest.main()
