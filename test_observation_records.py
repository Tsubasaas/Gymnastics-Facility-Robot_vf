"""Offline correspondence tests. Camera/model calls are explicitly mocked."""
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import report_data

# No camera, cloud call or robot SDK is used by this test suite.
with patch.dict("sys.modules", {"cv2": types.ModuleType("cv2"),
                                "numpy": types.ModuleType("numpy")}):
    import vision_coach as coach
import demo_voice


class CorrespondenceTests(unittest.TestCase):
    def test_no_record_does_not_fall_back_to_demo(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as d:
            self.assertIsNone(report_data.load_report(d))

    def test_record_keeps_identity_and_unknowns(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as d:
            raw = {"session_id": "one", "participants": ["A", "B"], "sections": [
                {"section": "3_udemawashi", "observation_id": "one:1",
                 "captured_at": [1, 2], "results": [{"name": "B", "ok": None,
                 "moving": None, "box": [1, 2, 3, 4]}],
                 "planned_feedback": [{"name": "B", "advice": "test"}]}]}
            Path(d, "taiso_one.json").write_text(json.dumps(raw), "utf-8")
            r = report_data.load_report(d)
            self.assertEqual([p["letter"] for p in r["participants"]], ["B"])
            j = r["judgments"]["B"]["3_udemawashi"]
            self.assertEqual(j["r"], "unknown")
            self.assertEqual(j["observation_id"], "one:1")
            self.assertIsNone(j["m"])
            self.assertEqual(r["spoken"][0]["status"], "planned_not_confirmed")
            self.assertFalse(r["participants"][0]["completed"])
            self.assertEqual(r["affects"], {})

    def test_path_traversal_rejected(self):
        for name in ("../secret.json", "taiso_../../secret.json"):
            with self.assertRaises(ValueError):
                report_data.load_report(".", name)
        with self.assertRaises(ValueError):
            report_data.evidence_path(".", "../secret.jpg")

    def test_stale_frame_not_reused(self):
        g = coach.FrameGrabber(types.SimpleNamespace())
        g.frame = Mock()
        g.captured_at = time.time() - 3
        self.assertIsNone(g.latest())
        self.assertEqual(g.sample(), (None, None))
        g.captured_at = time.time()
        self.assertIsNotNone(g.latest())

    def test_unknown_is_not_praised_or_marked_still(self):
        d = coach.Director("a" * 32)
        self.assertEqual(d.decide("3_udemawashi", [
            {"name": "A", "ok": None, "issue": None, "moving": None}]), [])
        self.assertFalse(d.small)

    def test_sessions_and_files_are_separate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            with patch.object(coach, "RECORDS_DIR", Path(tmp)), \
                 patch.object(coach, "REPORT_PATH", Path(tmp) / "summary.json"):
                first, second = coach.Director("a" * 32), coach.Director("b" * 32)
                first.history.append(("3_udemawashi", [{"name": "A", "ok": True}], []))
                first._write_report(["A"])
                second._write_report([])
                self.assertEqual(second.history, [])
                self.assertEqual(len(list(Path(tmp).glob("taiso_*.json"))), 2)
                self.assertEqual(report_data.load_report(tmp, "taiso_" + "b" * 32 + ".json")["participants"], [])

    def test_model_duplicates_and_unknown_types(self):
        payload = [{"name": "A", "ok": True}, {"name": "A", "ok": True},
                   {"name": "B", "ok": "false", "issue": "circle_small"}, 9]
        rsp = types.SimpleNamespace(status_code=200, output=types.SimpleNamespace(choices=[
            types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps(payload)))]))
        with patch.dict("sys.modules", {"dashscope": types.ModuleType("dashscope")}), \
             patch.object(coach, "_vlm_call", return_value=rsp), \
             patch.object(coach, "_encode", return_value="image"), \
             patch.object(coach, "_motion_score", return_value=None), \
             patch.object(coach.rules, "NAMES", ["A", "B"]):
            result = coach.vlm_judge("3_udemawashi", [object()])
        self.assertEqual([r["name"] for r in result], ["B"])
        self.assertIsNone(result[0]["ok"])
        self.assertIsNone(result[0]["moving"])

    def test_old_session_feedback_is_not_spoken(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            obs = Path(tmp) / "obs.json"
            obs.write_text(json.dumps({"session_id": "old", "section": "test",
                                      "wall": time.time(), "observations": [{"text": "wrong"}]}))
            v = demo_voice.VoiceCoach.__new__(demo_voice.VoiceCoach)
            v._session_id, v._fb_windows, v._fired = "new", {"key": [("test", 0, 10)]}, set()
            with patch.object(demo_voice, "OBS_PATH", obs), \
                 patch.object(demo_voice, "_tts_busy", return_value=False), \
                 patch.object(v, "coach_feedback") as speak:
                v._try_feedback("key", 2)
                speak.assert_not_called()


if __name__ == "__main__":
    unittest.main()

