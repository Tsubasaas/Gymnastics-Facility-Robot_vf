# -*- coding: utf-8 -*-
"""视觉教练的浏览器控制台(vision_coach.py --web 时启动,只监听本机)。

    GET  /                  控制台页面(vision_ui.html)
    GET  /report            场次报告 dashboard(?fmt=pdf|csv|json 打开后自动打印/下载)
    GET  /stream.mjpg       标注后的实时画面(MJPEG)
    GET  /api/state         视觉状态 + 演出程序状态(前端每 0.5s 轮询)
    POST /api/robot/start   在新控制台窗口启动真机演出程序 demo_robot.py
    POST /api/participants  保存参与者名单(字母 + 昵称),由 vision_coach 注册

启动演出程序 = 原来"终端 2"那条命令。安全确认流程不变:ENABLE REAL ROBOT
和每个动作的 RUN 随机码仍然在弹出的控制台窗口里由现场人员输入,网页不代输。
紧急停止 / 导入音乐目前只有界面。报告默认读取真实记录，mode=sample 保留四人案例。
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from report_data import load_report, evidence_path

import cv2

PROJECT_DIR = Path(__file__).resolve().parent
UI_FILE = PROJECT_DIR / "vision_ui.html"
REPORT_FILE = PROJECT_DIR / "vision_report.html"   # 真实记录 + 显式四人示例模式
DEFAULT_PORT = int(os.environ.get("VISION_WEB_PORT", "8765"))
STREAM_FPS = 12


class _Frame:
    """主循环发布最新一帧 JPEG,MJPEG 连接各自取用。"""

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg: bytes | None = None
        self.seq = 0
        self._last = 0.0

    def publish(self, bgr) -> None:
        now = time.time()
        if now - self._last < 1.0 / STREAM_FPS:
            return
        self._last = now
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        with self.cond:
            self.jpeg = buf.tobytes()
            self.seq += 1
            self.cond.notify_all()


class RobotLauncher:
    """在独立控制台窗口里启动 demo_robot.py,并跟踪其是否仍在运行。"""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.cmdline: list[str] = []
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        with self.lock:
            run = self.running()
            return {
                "running": run,
                "pid": self.proc.pid if self.proc else None,
                "started_at": self.started_at if self.proc else None,
                "exit_code": None if (run or not self.proc) else self.proc.returncode,
                "cmdline": " ".join(self.cmdline[1:]) if self.cmdline else "",
            }

    def start(self, opts: dict) -> dict:
        speed = float(opts.get("speed", 10))
        if speed not in (10.0, 45.0):
            raise ValueError("速度只允许 10 或 45 °/s")
        iface = str(opts.get("interface", "192.168.123.222")).strip()
        ipaddress.ip_address(iface)            # 不是合法 IP 直接报错
        dof = int(opts.get("dof", 29))
        if dof not in (23, 29):
            raise ValueError("DOF 只允许 23 或 29")
        cmd = [sys.executable, str(PROJECT_DIR / "demo_robot.py"),
               "--robot-dof", str(dof), "--interface", iface,
               "--max-speed-deg", f"{speed:g}", "--execute"]
        if opts.get("waist"):
            cmd.append("--allow-waist-roll-pitch")
        if opts.get("speaker"):
            cmd.append("--robot-speaker")
        if not opts.get("bgm", True):
            cmd.append("--no-bgm")
        with self.lock:
            if self.running():
                raise RuntimeError("演出程序已经在运行(PID %d)" % self.proc.pid)
            env = dict(os.environ, PYTHONUTF8="1")
            env.pop("PC2_PASS", None)          # 演出程序用不到设备密码,不外传
            self.proc = subprocess.Popen(
                cmd, cwd=str(PROJECT_DIR), env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            self.started_at = time.time()
            self.cmdline = cmd
            print(f"[web] 已启动演出程序 PID {self.proc.pid}: {' '.join(cmd[1:])}")
        return self.status()


def serve(get_state, port: int = DEFAULT_PORT, open_browser: bool = True,
          actions: dict | None = None):
    """启动后台 HTTP 服务;返回 (frame_sink, launcher)。get_state() -> dict。
    actions = {"/api/xxx": fn(opts: dict) -> dict},额外的 POST 接口
    (fn 抛 ValueError 时返回 400 和错误信息)。"""
    frame = _Frame()
    launcher = RobotLauncher()
    routes = {"/api/robot/start": launcher.start, **(actions or {})}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):            # 轮询太频繁,不刷屏
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/report"):
                body = (REPORT_FILE if path == "/report" else UI_FILE).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/evidence/"):
                try:
                    body = evidence_path(PROJECT_DIR / "records", path.removeprefix("/evidence/")).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (OSError, ValueError):
                    self._json(404, {"error": "Evidence unavailable"})
            elif path == "/api/report":
                try:
                    record_id = parse_qs(urlsplit(self.path).query).get("id", [None])[0]
                    report = load_report(PROJECT_DIR / "records", record_id)
                    self._json(200 if report else 404, report or {"error": "No session records"})
                except (OSError, ValueError, KeyError, TypeError) as e:
                    self._json(400, {"error": str(e)})
            elif path == "/api/state":
                state = get_state()
                state["robot"] = launcher.status()
                self._json(200, state)
            elif path == "/stream.mjpg":
                self._stream()
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            # 只接受本页面发出的 JSON 请求(自定义头会触发 CORS 预检,
            # 其他网站无法跨站替你点"启动")
            if self.headers.get("X-Coach-Console") != "1":
                return self._json(403, {"error": "forbidden"})
            handler = routes.get(self.path)
            if handler is None:
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", "0"))
                opts = json.loads(self.rfile.read(n) or b"{}")
                self._json(200, handler(opts))
            except (ValueError, RuntimeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                while True:
                    with frame.cond:
                        frame.cond.wait_for(lambda: frame.seq != seen, timeout=2.0)
                        jpeg, seen = frame.jpeg, frame.seq
                    if jpeg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                                     + jpeg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[web] 控制台: {url}")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return frame, launcher
