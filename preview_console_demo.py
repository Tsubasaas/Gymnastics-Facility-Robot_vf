"""Offline console illustration: python preview_console_demo.py (port 8082).

All people, judgments and expressions are fictional; no camera, cloud or robot.
Uses the production console renderer with a fixed sample state.
"""
import json
from http.server import ThreadingHTTPServer
from preview_console import Handler, ROOT

SAMPLE_TIME = 1794898800
PEOPLE = [
    {"name": "A", "action": "双臂上举", "affect": "兴趣"},
    {"name": "B", "action": "双臂上举", "affect": "愉悦"},
    {"name": "C", "action": "站立", "affect": "?"},
]
STATE = {
    "preview_only": True, "now": SAMPLE_TIME, "clock_active": False,
    "camera_ok": True, "judging": False, "section": None, "beat": None,
    "people": PEOPLE, "people_age": 0, "alert": [],
    "last_judge": {"section": "4_扩胸（示例）", "results": [
        {"name": "A", "ok": True, "moving": True},
        {"name": "B", "ok": False, "moving": True, "issue": "arms_low"},
        {"name": "C", "ok": None, "moving": None}]},
    "events": [{"t": SAMPLE_TIME, "kind": "info", "text":
                "演示示例：人物、识别框、动作判断和表情均为虚构，未连接设备。"}],
    "speech_events": [], "affect_log": [
        {**p, "t": SAMPLE_TIME, "mood": "积极" if p["name"] != "C" else "未知"}
        for p in PEOPLE],
    "affect_stats": {"A": {"pos": 1, "neg": 0}, "B": {"pos": 1, "neg": 0},
                     "C": {"pos": 0, "neg": 0, "unknown": 1}},
    "affects": {"兴趣": {"emoji": "👀", "mood": "积极", "ja": "興味"},
                "愉悦": {"emoji": "😄", "mood": "积极", "ja": "喜び"}},
    "names": ["A", "B", "C"], "nicknames": {n: "示例参与者 " + n for n in "ABC"},
    "added_at": {}, "max_participants": 8,
    "models": {"judge": "示例数据", "preview": "示例数据"},
    "actions_ja": {}, "robot": {"running": False}, "sections": [],
}


class DemoHandler(Handler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            return self.respond(200, json.dumps(STATE, ensure_ascii=False).encode(), "application/json")
        if path == "/demo-frame.svg":
            return self.respond(200, (ROOT / "preview/console_demo_frame.svg").read_bytes(), "image/svg+xml")
        if path == "/":
            html = (ROOT / "vision_ui.html").read_text("utf-8")
            html = html.replace('src="/stream.mjpg"', 'src="/demo-frame.svg"')
            html = html.replace("applyLang();\npoll();", 'lang = "zh"; applyLang();\npoll();')
            html = html.replace("<body>", '<body><div style="padding:12px 24px;background:#fff2cd;color:#67480b;font-weight:700;text-align:center">演示示例 · 虚构人物 / 模拟识别数据 · 未连接摄像头、模型或机器人</div>')
            html = html.replace("render(S);", 'render(S); $("dot-cam").className="dot"; document.querySelector(\'[data-i18n="chip_cam"]\').textContent="示例画面";')
            return self.respond(200, html.encode(), "text/html; charset=utf-8")
        return self.respond(404, b"Demo only", "text/plain")


if __name__ == "__main__":
    print("Offline demonstration: http://127.0.0.1:8082/")
    ThreadingHTTPServer(("127.0.0.1", 8082), DemoHandler).serve_forever()
