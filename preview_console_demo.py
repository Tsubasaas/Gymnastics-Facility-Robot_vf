"""Offline console illustration: python preview_console_demo.py (port 8082).

All people, judgments and expressions are fictional; no camera, cloud or robot.
Uses the production console renderer with a fixed sample state.
"""
import json
from urllib.parse import urlsplit, parse_qs
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
    "last_judge": {"section": "4_Chest opener (sample)", "results": [
        {"name": "A", "ok": True, "moving": True},
        {"name": "B", "ok": False, "moving": True, "issue": "arms_low"},
        {"name": "C", "ok": None, "moving": None}]},
    "events": [{"t": SAMPLE_TIME, "kind": "info", "text":
                "DEMO: Fictional participants, boxes, judgments and expressions. No devices connected."}],
    "speech_events": [], "affect_log": [
        {**p, "t": SAMPLE_TIME, "mood": "积极" if p["name"] != "C" else "未知"}
        for p in PEOPLE],
    "affect_stats": {"A": {"pos": 1, "neg": 0}, "B": {"pos": 1, "neg": 0},
                     "C": {"pos": 0, "neg": 0, "unknown": 1}},
    "affects": {"兴趣": {"emoji": "👀", "mood": "积极", "ja": "興味"},
                "愉悦": {"emoji": "😄", "mood": "积极", "ja": "喜び"}},
    "names": ["A", "B", "C"], "nicknames": {n: "Sample participant " + n for n in "ABC"},
    "added_at": {}, "max_participants": 8,
    "models": {"judge": "Sample data", "preview": "Sample data"},
    "actions_ja": {"双臂上举": "両腕を上げる", "站立": "立位"}, "robot": {"running": False}, "sections": [],
}


TRANSLATIONS = {
    "zh": {
        "DEMONSTRATION ONLY · Fictional participants and sample results · No camera, AI model or robot connected": "演示示例 · 虚构人物与识别数据 · 未连接摄像头、模型或机器人",
        "DEMO · Fictional participants and results": "演示示例 · 虚构人物与识别结果",
        "Illustration only / No camera or AI inference / Console layout preview": "示意图 / 未调用摄像头或视觉模型 / 仅展示界面布局",
        "DEMO: Fictional participants, boxes, judgments and expressions. No devices connected.": "演示示例：人物、识别框、动作判断与表情均为虚构，未连接设备。",
        "Sample participant": "示例参与者", "Sample data": "示例数据", "Sample frame": "示例画面",
        "4_Chest opener (sample)": "4_扩胸（示例）", "OK / Interest": "达标 / 兴趣",
        "Arms low / Pleasure": "手臂偏低 / 愉悦", "Uncertain / Unknown": "不确定 / 未知",
    },
    "ja": {
        "DEMONSTRATION ONLY · Fictional participants and sample results · No camera, AI model or robot connected": "デモ表示 · 架空の参加者と認識結果 · カメラ・AIモデル・ロボットは未接続",
        "DEMO · Fictional participants and results": "デモ表示 · 架空の参加者と認識結果",
        "Illustration only / No camera or AI inference / Console layout preview": "イラスト / カメラ撮影・AI推論なし / 画面構成のサンプル",
        "DEMO: Fictional participants, boxes, judgments and expressions. No devices connected.": "デモ表示：参加者・認識枠・動作判定・表情はすべて架空です。機器は未接続です。",
        "Sample participant": "サンプル参加者", "Sample data": "サンプルデータ", "Sample frame": "サンプル画像",
        "4_Chest opener (sample)": "4_胸をそらす（サンプル）", "OK / Interest": "OK / 興味",
        "Arms low / Pleasure": "腕が低い / 喜び", "Uncertain / Unknown": "判定不確実 / 不明",
    },
}


def translate(text, language):
    for source, target in TRANSLATIONS.get(language, {}).items():
        text = text.replace(source, target)
    return text


class DemoHandler(Handler):
    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        language = parse_qs(url.query).get("lang", ["en"])[0]
        if language not in ("en", "zh", "ja"):
            language = "en"
        if path == "/api/state":
            return self.respond(200, translate(json.dumps(STATE, ensure_ascii=False), language).encode(), "application/json")
        if path == "/demo-frame.svg":
            return self.respond(200, translate((ROOT / "preview/console_demo_frame.svg").read_text("utf-8"), language).encode(), "image/svg+xml")
        if path == "/":
            html = (ROOT / "vision_ui.html").read_text("utf-8")
            html = html.replace('src="/stream.mjpg"', f'src="/demo-frame.svg?lang={language}"')
            html = html.replace('fetch("/api/state",', f'fetch("/api/state?lang={language}",')
            html = html.replace("applyLang();\npoll();", f'lang = "{language}"; applyLang();\npoll();')
            html = html.replace("<body>", '<body><div style="padding:12px 24px;background:#fff2cd;color:#67480b;font-weight:700;text-align:center">DEMONSTRATION ONLY · Fictional participants and sample results · No camera, AI model or robot connected</div>')
            html = html.replace("render(S);", 'render(S); $("dot-cam").className="dot"; document.querySelector(\'[data-i18n="chip_cam"]\').textContent="Sample frame";')
            return self.respond(200, translate(html, language).encode(), "text/html; charset=utf-8")
        if path == "/report":
            # Force the built-in fictional report; never read personal records.
            html = (ROOT / "vision_report.html").read_text("utf-8")
            html = html.replace('const params = new URLSearchParams(location.search);', 'const params = new URLSearchParams(location.search); params.set("mode", "sample");')
            return self.respond(200, html.encode(), "text/html; charset=utf-8")
        return self.respond(404, b"Demo only", "text/plain")


if __name__ == "__main__":
    print("Offline demonstration: http://127.0.0.1:8082/")
    ThreadingHTTPServer(("127.0.0.1", 8082), DemoHandler).serve_forever()
