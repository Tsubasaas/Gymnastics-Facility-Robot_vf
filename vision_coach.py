# -*- coding: utf-8 -*-
"""视觉互动教练(独立进程):看现场的人 → 判达标 → 写 observations.json。

链路:
  摄像头(默认外接Brio,经 robot_camera.ExternalSource)→ 按节采3个关键帧
  → 第8拍末发起 Qwen-VL 评判(胸前字母认人 A/B/C,标准见 vision_rules)
  → 选出本节最多1条反馈 → 原子写 observations.json
  → demo_voice.py 在该节第9~14拍的空窗读取并播报。

时钟:优先读 demo_voice 写的 vision_clock.json(同一播放时钟,暂停/重播都跟随);
读不到时 --standalone 手动回车对齐 demo 开始,按写死时间轴推算。

用法(.venv-real 环境,和 demo_voice 同时跑):
  .\.venv-real\Scripts\python.exe vision_coach.py            # 外接Brio + 时钟文件
  .\.venv-real\Scripts\python.exe vision_coach.py --cam 0    # 笔记本摄像头
  .\.venv-real\Scripts\python.exe vision_coach.py --standalone --show
挂了也不影响 demo:语音侧读不到新 observations = 静默(没识别到人就不出声)。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import threading
import time
import uuid
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import cv2
import numpy as np

import demo_viewer
import vision_rules as rules
from session_events import append_event, read_events

PROJECT_DIR = Path(__file__).resolve().parent
CLOCK_PATH = PROJECT_DIR / "vision_clock.json"
OBS_PATH = PROJECT_DIR / "observations.json"
REPORT_PATH = PROJECT_DIR / "session_report.json"   # 班后汇报(语音侧读)
RECORDS_DIR = PROJECT_DIR / "records"               # 每场留档

# qwen3-vl-plus=商用旗舰(实测640px三帧1.1s);备选 qwen3-vl-235b-a22b-instruct
# /qwen3-vl-flash(0.8s);qwen-vl-max-latest 在国际版 endpoint 无权限。
VLM_MODEL = os.environ.get("VISION_MODEL", "qwen3-vl-plus")
SEND_WIDTH = 640          # 发给 VLM 前缩到这个宽度(实测960px三张要4.7s,640px仅1.0s)
FULL_KEYS = {"0_demo_full", "7_demo_full"}   # 仿真通し / 真机p7通し


def _api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        from llm_motion import API_KEY
        key = API_KEY
    return key


# ---------------- 帧源:后台线程持续取最新帧 ----------------

class FrameGrabber:
    def __init__(self, args):
        self.args = args
        self.frame = None          # 最新 BGR 帧
        self.captured_at = 0.0
        self.lock = threading.Lock()
        self.ok = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def latest(self):
        with self.lock:
            return (None if self.frame is None or time.time() - self.captured_at > 2.0
                    else self.frame.copy())

    def sample(self):
        with self.lock:
            if self.frame is None or time.time() - self.captured_at > 2.0:
                return None, None
            return self.frame.copy(), self.captured_at

    def _run(self):
        if self.args.cam is not None:
            cap = cv2.VideoCapture(self.args.cam)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            print(f"[cam] 本机摄像头 {self.args.cam}")
            while True:
                ret, frame = cap.read()
                if ret:
                    with self.lock:
                        self.frame, self.ok = frame, True
                        self.captured_at = time.time()
                else:
                    time.sleep(0.2)
        else:
            from robot_camera import ExternalSource
            src = ExternalSource()
            try:
                src.open()
            except Exception as e:
                # 不自动回退本机摄像头:打开用户设备必须由用户显式选择(--cam)
                print(f"[cam] 机器人Brio连不上:{e}")
                print("[cam] 已停止。若要改用笔记本摄像头,请显式加 --cam 0 重新运行。")
                return
            print("[cam] 机器人外接 Brio(TCP)")
            while True:
                jpeg, _err = src.fetch()
                if jpeg is None:
                    time.sleep(0.2)
                    continue
                img = cv2.imdecode(np.frombuffer(jpeg, np.uint8),
                                   cv2.IMREAD_COLOR)
                if img is not None:
                    with self.lock:
                        self.frame, self.ok = img, True
                        self.captured_at = time.time()


# ---------------- 时钟:跟随 demo_voice 的播放时钟 ----------------

class Clock:
    """返回 (section_key, 节内拍号) 或 (None, None)。"""

    def __init__(self, standalone: bool):
        self.standalone = standalone
        self.t0 = None
        self.active = False     # 演出时钟是否在走(用于演出中暂停预览标注)
        self.session_id = uuid.uuid4().hex if standalone else None
        # 通し时间轴 [(key, start_s, end_s)],只留 ①~⑤
        self.timeline = [(k, s, e) for k, _n, s, e in demo_viewer.section_timeline()]

    def start_standalone(self):
        input(">> demo 开始的瞬间按回车对齐时钟 <<")
        self.t0 = time.time()

    def _from_full_t(self, t: float):
        for key, start, end in self.timeline:
            if start <= t < end:
                return key, (t - start) / demo_viewer.BEAT
        return None, None

    def now(self):
        if self.standalone:
            if self.t0 is None:
                self.active = False
                return None, None
            elapsed = time.time() - self.t0
            self.active = elapsed <= max(end for _, _, end in self.timeline)
            return self._from_full_t(elapsed) if self.active else (None, None)
        try:
            raw = json.loads(CLOCK_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            self.active = False
            return None, None
        age = time.time() - raw.get("wall", 0)
        session_id = raw.get("session_id")
        self.session_id = (session_id if isinstance(session_id, str)
                           and len(session_id) == 32
                           and all(c in "0123456789abcdef" for c in session_id) else None)
        self.active = age <= 2.0
        if age > 2.0:                      # 时钟停更:demo 没在跑
            return None, None
        t = raw["t"] + max(0.0, age)       # 用墙钟外推,误差≤写入间隔
        key = raw["key"]
        if key in FULL_KEYS:
            return self._from_full_t(t)
        if key in {k for k, _, _ in self.timeline}:
            return key, t / demo_viewer.BEAT - 1.0   # 单节网格有1拍起手偏移
        return None, None


# ---------------- VLM 评判 ----------------

def _encode(frame) -> str:
    h, w = frame.shape[:2]
    if w > SEND_WIDTH:
        frame = cv2.resize(frame, (SEND_WIDTH, int(h * SEND_WIDTH / w)))
    _ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return "data:image/jpeg;base64," + base64.b64encode(buf).decode()


def _vlm_call(model: str, content: list):
    """调用 DashScope 多模态接口。qwen3.5 及之后的通用模型默认开思考、慢一倍,
    这里一律关掉(qwen3-vl-* 不支持该参数,不传)。"""
    import dashscope
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
    extra = {} if model.startswith("qwen3-vl") else {"enable_thinking": False}
    return dashscope.MultiModalConversation.call(
        model=model, api_key=_api_key(), **extra,
        messages=[{"role": "user", "content": content}])


# "有没有在动"不交给 VLM(实测三张相同照片它仍判"在动"),改为本地像素比较:
# 在该人边界框内,把各关键帧缩成小灰度图、模糊去噪后求相邻帧平均绝对差,
# 取最大值。低于阈值 = 几帧之间几乎没变化 = 没在跟着做。
# 阈值 0~255 灰度;日志会打印每人的 motion 值,现场据此校准
# (相机噪声约 1~3,跟做时手臂区域通常明显更高)。
STILL_DIFF = float(os.environ.get("VISION_STILL_DIFF", "4.0"))


def _motion_score(frames: list, box) -> float | None:
    if len(frames) < 2 or not (isinstance(box, list) and len(box) == 4):
        return None
    try:
        x1, y1, x2, y2 = [max(0, min(1000, int(v))) for v in box]
    except (TypeError, ValueError):
        return None
    if x2 - x1 < 20 or y2 - y1 < 20:
        return None
    crops = []
    for f in frames:
        h, w = f.shape[:2]
        c = f[y1 * h // 1000:y2 * h // 1000, x1 * w // 1000:x2 * w // 1000]
        c = cv2.cvtColor(cv2.resize(c, (96, 128)), cv2.COLOR_BGR2GRAY)
        crops.append(cv2.GaussianBlur(c, (5, 5), 0).astype(np.float32))
    return max(float(np.mean(np.abs(a - b))) for a, b in zip(crops, crops[1:]))


def _is_moving(frames: list, box, name: str) -> bool:
    score = _motion_score(frames, box)
    if score is None:          # 没框/框无效:拿不准 = 当作在动,防误提醒
        return True
    print(f"[motion] {name} = {score:.1f}(阈值 {STILL_DIFF})")
    return score >= STILL_DIFF


def _build_prompt(sec: dict) -> str:
    issue_desc = ",".join(sec["issues"])
    letters = "、".join(rules.NAMES)
    return (
        "这是广播体操演示现场按时间顺序采样的几张照片。场上最多有"
        f"{len(rules.NAMES)}位参与者,胸前贴有大字母标识:{letters}。\n"
        f"当前节:{sec['title']}。{sec['standard']}\n"
        "只统计能看到上半身(含胸口)的人;只露出头顶、手脚或被大面积遮挡的"
        "不算,宁缺毋滥,绝不要凑数。只评正在跟着做操的参与者;明显只是"
        "旁观、坐着、路过的人不要输出。\n"
        "任务:对每一位符合条件的参与者:\n"
        "1. 只用清晰可读的胸前字母确定身份;看不清时不要输出该人，禁止按位置猜测身份。\n"
        "2. 综合几张照片判断是否达标——取其中动作幅度最大的一张来判断,"
        "单张照片处于动作中途是正常的,不要因此判不达标;\n"
        f"3. 若明确不达标,issue 从这些里选一个:{issue_desc}。"
        "没有把握时 ok=null，issue=null，不要推断为达标。\n"
        '4. 再给一个 energy 字段:动作有力、精神饱满 = "high";'
        '达标但明显蔫、幅度偏小 = "low";拿不准填 "high"。\n'
        "5. 再给一个 box 字段:该人在最后一张照片里的整体边界框 [x1,y1,x2,y2],"
        "坐标归一化到 0~1000。\n"
        "只输出JSON数组,不要其他文字,例如:\n"
        '[{"name":"A","ok":true,"energy":"high","box":[100,80,420,1000]},'
        '{"name":"B","ok":false,"issue":"...","energy":"low","box":[500,90,800,1000]}]\n'
        "画面里没有人则输出 []。")


def vlm_judge(section_key: str, frames: list) -> list:
    """返回 [{"name":"A","ok":bool,"issue":str|None}, ...];失败返回 []。"""
    import dashscope
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"

    sec = rules.SECTIONS[section_key]
    content = [{"image": _encode(f)} for f in frames]
    content.append({"text": _build_prompt(sec)})
    t0 = time.time()
    try:
        rsp = _vlm_call(VLM_MODEL, content)
    except Exception as e:
        print(f"[vlm] 调用异常:{e}")
        return None
    if rsp.status_code != 200:
        print(f"[vlm] {rsp.code}: {rsp.message}")
        return None
    text = rsp.output.choices[0].message.content
    if isinstance(text, list):             # dashscope 返回 [{'text': ...}]
        text = "".join(p.get("text", "") for p in text)
    text = text.strip().removeprefix("```json").removeprefix("```").strip("`\n ")
    try:
        data = json.loads(text)
    except ValueError:
        print(f"[vlm] JSON解析失败:{text[:120]!r}")
        return None
    if not isinstance(data, list):
        return None
    out = []
    counts = {}
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip().upper()
            counts[name] = counts.get(name, 0) + 1
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().upper()
        if name not in rules.NAMES or counts[name] != 1:
            continue
        issue = item.get("issue")
        if issue not in sec["issues"]:
            issue = None
        score = _motion_score(frames, item.get("box"))
        ok = item.get("ok")
        if not isinstance(ok, bool) or (ok is False and issue is None):
            ok = None
        if ok is not False:
            issue = None
        out.append({"name": name, "ok": ok, "raw_model_result": item,
                    "issue": issue,
                    "energy": str(item.get("energy", "high")).lower(),
                    "box": item.get("box"), "motion_score": score,
                    "moving": None if score is None else score >= STILL_DIFF})
    print(f"[vlm] {section_key} {time.time() - t0:.1f}s -> {out}")
    return out


# 画面标注(认人+动作+表情)。实测(真实画面、关思考,3次平均):
#   qwen3.7-flash 2.65s / qwen3.7-plus 3.28s / qwen3-vl-plus 3.67s /
#   qwen3.8-flash 13.5s(反而慢);qwen3-vl-flash 在部分国际版账号上 404。
PREVIEW_MODEL = os.environ.get("VISION_PREVIEW_MODEL", "qwen3.7-flash")
PREVIEW_INTERVAL = 4.0     # 空闲时每隔几秒刷新一次画面标注
# 画面标注里的动作候选(与广播体操各节对应)与表情两分类(BGR 颜色)
PREVIEW_ACTIONS = ["站立", "双臂上举", "双臂侧平举", "抡臂", "扩胸后仰",
                   "体侧屈", "弯腰前倾", "挥手", "走动", "其他"]
MOOD_COLORS = {"积极": (80, 220, 80), "消极": (60, 60, 255), "?": (180, 180, 180)}

# 表情判定依据 PGC-ARS(Philadelphia Geriatric Center Affect Rating Scale,
# Lawton et al., 1996):养老机构用的观察式情绪量表,3类积极 + 3类消极。
# 原量表是受训观察者在约10分钟内按持续时长打分;这里只借用其类别与
# 可观察特征做单帧粗分类,不是量表的正式施测,结果仅供现场参考。
# 类别 → (积极/消极, 可观察特征, 日语显示名)
PGC_AFFECTS = {
    "愉悦": ("积极", "微笑、大笑、开心地唱歌或拍手", "喜び"),
    "兴趣": ("积极", "目光追随动作或机器人、专注的表情、点头回应、积极跟做", "興味"),
    "满足": ("积极", "放松的姿势与面部、安静平和、动作舒缓", "満足"),
    "愤怒": ("消极", "咬牙、做鬼脸、抿嘴、眯眼、怒视、用力挥手推开", "怒り"),
    "焦虑": ("消极", "皱眉、紧张的面部肌肉、坐立不安、反复搓手、四处张望、叹气、退缩",
             "不安"),
    "悲伤": ("消极", "哭泣、流泪、叹气、嘴角下垂、低头垂眼、面部松垮无神", "悲しみ"),
}


def _affect_guide() -> str:
    return "".join(f"  · {k}({v[0]}):{v[1]};\n" for k, v in PGC_AFFECTS.items())


# 每类情绪的 emoji(画在人物框右上角,底下垫积极绿/消极红/看不清灰的圆)
AFFECT_EMOJI = {"愉悦": "😄", "兴趣": "👀", "满足": "😌",
                "愤怒": "😠", "焦虑": "😟", "悲伤": "😢", "?": "❔"}
EMOJI_SIZE = 56
EMOJI_FONT = r"C:\Windows\Fonts\seguiemj.ttf"   # Windows 自带彩色 emoji 字体
_emoji_cache: dict = {}


def _emoji_sprite(ch: str):
    """emoji → BGRA 小图(带圆形底色,按情绪上色);缺 Pillow/字体时返回 None。"""
    if ch in _emoji_cache:
        return _emoji_cache[ch]
    sprite = None
    try:
        from PIL import Image, ImageDraw, ImageFont
        affect = next((k for k, v in AFFECT_EMOJI.items() if v == ch), "?")
        mood = PGC_AFFECTS[affect][0] if affect in PGC_AFFECTS else "?"
        b, g, r = MOOD_COLORS[mood]
        pad = 6
        size = EMOJI_SIZE + pad * 2
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((0, 0, size - 1, size - 1), fill=(r, g, b, 200))
        if ch == "❔":     # 灰色 ❔ 压在灰底上看不见,改画白色粗体问号
            font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf",
                                      int(EMOJI_SIZE * 0.75))
            draw.text((size / 2, size / 2), "?", font=font,
                      fill=(255, 255, 255, 255), anchor="mm")
        else:
            font = ImageFont.truetype(EMOJI_FONT, int(EMOJI_SIZE * 0.8))
            draw.text((size / 2, size / 2), ch, font=font, embedded_color=True,
                      anchor="mm")
        sprite = cv2.cvtColor(np.array(img), cv2.COLOR_RGBA2BGRA)
    except Exception as e:
        print(f"[show] emoji 不可用,仅显示文字:{e}")
    _emoji_cache[ch] = sprite
    return sprite


def _overlay(dst, sprite, x: int, y: int) -> None:
    """把 BGRA 小图按 alpha 混合贴到 dst 的 (x,y),越界部分自动裁掉。"""
    h, w = sprite.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dst.shape[1], x + w), min(dst.shape[0], y + h)
    if x0 >= x1 or y0 >= y1:
        return
    s = sprite[y0 - y:y1 - y, x0 - x:x1 - x]
    a = s[:, :, 3:4].astype(np.float32) / 255.0
    roi = dst[y0:y1, x0:x1]
    roi[:] = (s[:, :, :3] * a + roi * (1 - a)).astype(dst.dtype)


LABEL_W = 250   # 标签深色底的宽度


def _draw_person(disp, p: dict) -> None:
    """在 960x540 的显示帧上画一个人:框 + 左上角标签(名字/动作/表情) + emoji。"""
    h, w = disp.shape[:2]
    x1, y1, x2, y2 = (int(p["box"][0] / 1000 * w), int(p["box"][1] / 1000 * h),
                      int(p["box"][2] / 1000 * w), int(p["box"][3] / 1000 * h))
    color = (0, 255, 0) if p["name"] in rules.NAMES else (0, 200, 255)
    cv2.rectangle(disp, (x1, y1), (x2, y2), color, 2)
    # 标签垫半透明深色底,白墙背景上也看得清
    mood = p.get("mood", "?")
    affect = p.get("affect", "?")
    ly = max(0, y1)
    roi = disp[ly:ly + 92, x1:x1 + LABEL_W]
    roi[:] = (roi * 0.35).astype(roi.dtype)
    cv2.putText(disp, p["name"], (x1 + 6, ly + 32),
                cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 3)
    action = p.get("action", "")
    cv2.putText(disp, ACTION_JA.get(action, action), (x1 + 6, ly + 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    label = MOOD_JA.get(mood, mood)
    if affect in PGC_AFFECTS:
        label += f"({PGC_AFFECTS[affect][2]})"
    cv2.putText(disp, f"表情:{label}", (x1 + 6, ly + 86),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                MOOD_COLORS.get(mood, MOOD_COLORS["?"]), 2)
    # emoji 放框右上角;框太窄时挪到标签右侧,不压住文字
    sprite = _emoji_sprite(AFFECT_EMOJI.get(affect, "❔"))
    if sprite is not None:
        ex = max(x2 - sprite.shape[1] - 4, x1 + LABEL_W + 4)
        _overlay(disp, sprite, ex, ly + 4)
# 调试窗口上的显示文字(日语);VLM 提示词和内部键仍用中文
ACTION_JA = {"站立": "直立", "双臂上举": "両腕を上げる", "双臂侧平举": "両腕を横に",
             "抡臂": "腕を回す", "扩胸后仰": "胸をそらす", "体侧屈": "体を横に曲げる",
             "弯腰前倾": "前かがみ", "挥手": "手を振る", "走动": "歩いている",
             "其他": "その他"}
MOOD_JA = {"积极": "ポジティブ", "消极": "ネガティブ", "?": "?"}


def vlm_locate(frame) -> list:
    """定位画面里的人:[{"name":"A|?","box":[x1,y1,x2,y2] 0~1000}];失败 []。"""
    import dashscope
    dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"

    letters = "、".join(rules.NAMES)
    prompt = (
        f"找出画面中每一位能看到脸或上半身的人,他们胸前可能贴着大字母标识({letters})。\n"
        "对每个人给出:\n"
        "- box: 该人整体的边界框 [x1,y1,x2,y2],坐标归一化到 0~1000;\n"
        f'- name: 胸前字母,看不清填 "?";\n'
        f"- action: 当前动作,从这些里选最接近的一个:{'、'.join(PREVIEW_ACTIONS)};\n"
        "- affect: 按 Philadelphia Geriatric Center Affect Rating Scale(PGC-ARS)"
        "的6类情绪,选画面中最明显的一类:\n"
        f"{_affect_guide()}"
        "  只有清晰可见的表情特征才选对应类别，不根据动作或站位推断情绪;拿不准填 '?'。"
        '戴口罩、背对、低头等看不清脸时填 "?"。\n'
        '只输出JSON数组,例如 [{"name":"A","box":[120,80,400,900],'
        '"action":"双臂上举","affect":"愉悦"}]。画面里没有人输出 []。')
    try:
        rsp = _vlm_call(PREVIEW_MODEL,
                        [{"image": _encode(frame)}, {"text": prompt}])
    except Exception:
        return None
    if rsp.status_code != 200:
        return None
    text = rsp.output.choices[0].message.content
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text)
    text = text.strip().removeprefix("```json").removeprefix("```").strip("`\n ")
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        box = item.get("box") or item.get("bbox_2d")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            box = [max(0, min(1000, int(v))) for v in box]
        except (TypeError, ValueError, OverflowError):
            continue
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        name = str(item.get("name", "?")).strip().upper()
        action = str(item.get("action", "")).strip()
        affect = str(item.get("affect", "?")).strip()
        if affect not in PGC_AFFECTS:
            affect = "?"
        out.append({"name": name if name in rules.NAMES else "?", "box": box,
                    "raw_model_result": item,
                    "action": action if action in PREVIEW_ACTIONS else "其他",
                    "affect": affect,
                    "mood": PGC_AFFECTS[affect][0] if affect != "?" else "?"})
    return out


# ---------------- 反馈编排:每节最多1条,轮流点名,人人有赞 ----------------

LAST_EVAL = list(rules.SECTIONS)[-1]       # 最后一个评判节(=④)


class Director:
    def __init__(self, session_id=None):
        self.session_id = session_id or uuid.uuid4().hex
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.nicknames = dict(rules.NICKNAMES)
        self.names = list(rules.NAMES)
        self.audit = {}
        self.featured: set[str] = set()    # 整场被点过名的人
        self.small: set[str] = set()       # 整场动作偏小的人(不达标或蔫)
        self.history: list = []            # [(节, 评判结果, 播出内容)]

    def decide(self, section_key: str, results: list) -> list:
        for r in results:                  # 汇报统计:不达标/没劲/没在动都算"动作偏小"
            if (not r["ok"] and r["issue"]) or r.get("energy") == "low" \
                    or r.get("moving") is False:
                self.small.add(r["name"])
        if not results:
            self.history.append((section_key, [], []))
            return []                      # 没识别到人 = 不出声
        sec = rules.SECTIONS[section_key]
        detected = {r["name"]: r for r in results}
        star = rules.ROTATION.get(section_key)

        still = [r for r in results if r.get("moving") is False]
        flagged = [r for r in results if not r["ok"] and r["issue"]]
        if still:                          # 有人完全没在动:最优先,先邀请一起动
            target = next((r for r in still if r["name"] not in self.featured),
                          still[0])
            obs = [{"name": target["name"], "advice": rules.STILL}]
        elif flagged:                      # 有明确不达标:纠正优先
            # 还没被点过名的优先(避免同一人连续两节被批评),其次主角
            target = (next((r for r in flagged
                            if r["name"] not in self.featured), None)
                      or next((r for r in flagged if r["name"] == star),
                              flagged[0]))
            obs = [{"name": target["name"],
                    "advice": sec["issues"][target["issue"]]}]
        else:                              # 全达标/拿不准:表扬或打气
            detected = {n: r for n, r in detected.items() if r.get("ok") is True}
            if not detected:
                self.history.append((section_key, results, []))
                return []
            name = star if star in detected and star not in self.featured \
                else None
            if name is None:               # 主角缺席/已点过:挑没点过名的
                fresh = [n for n in sorted(detected)
                         if n not in self.featured]
                name = fresh[0] if fresh else sorted(detected)[0]
            # 达标但不带劲 → 鼓励("もっとテンション上げて");否则表扬
            if detected[name].get("energy") == "low":
                obs = [{"name": name, "advice": rules.ENCOURAGE}]
            else:
                obs = [{"name": name, "advice": random.choice(rules.PRAISES)}]
        self.featured.add(obs[0]["name"])

        # 最后一节收尾:在场但还没被点过名的人补短表扬(上限1句,
        # 防止连播多句压到紧跟的结尾感谢语)
        if section_key == LAST_EVAL:
            for n in rules.NAMES:
                if n not in self.featured and n in detected and detected[n].get("ok") is True:
                    obs.append({"name": n,
                                "advice": random.choice(rules.PRAISES)})
                    self.featured.add(n)
                    break
        self.history.append((section_key, results, obs))
        return obs

    def _write_report(self, participants: list) -> None:
        """Persist the observations collected so far; completion is not inferred."""
        self.small = {r["name"] for _, results, _ in self.history for r in results
                      if (r.get("ok") is False and r.get("issue"))
                      or r.get("moving") is False or r.get("energy") == "low"}
        record = {
            "schema_version": 3,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "nicknames": self.nicknames,
            "still_threshold": STILL_DIFF,
            "models": {"judge": VLM_MODEL, "preview": PREVIEW_MODEL},
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "participants": participants,          # 本场识别到的人，不推断全程参与
            "small_movement": sorted(self.small),  # 全场动作偏小的人
            "sections": [
                {"section": s, "results": r, "planned_feedback": o,
                 **self.audit.get(s, {})}
                for s, r, o in self.history],
        }
        RECORDS_DIR.mkdir(exist_ok=True)
        rec_file = RECORDS_DIR / f"taiso_{self.session_id}.json"
        rec_tmp = rec_file.with_suffix(".tmp")
        rec_tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
        os.replace(rec_tmp, rec_file)
        tmp = REPORT_PATH.with_suffix(".rtmp")
        tmp.write_text(json.dumps({
            "session_id": self.session_id,
            "participants": participants,
            "small": sorted(self.small & set(participants)),
            "record_file": rec_file.name,
            "wall": time.time()}, ensure_ascii=False), "utf-8")
        os.replace(tmp, REPORT_PATH)
        print(f"[report] 已存档 {rec_file.name},参加{len(participants)}名,"
              f"动作偏小:{sorted(self.small)}")


def write_obs(section_key: str, seq: int, observations: list, session_id=None) -> None:
    tmp = OBS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "session_id": session_id, "section": section_key, "seq": seq, "wall": time.time(),
        "observation_id": f"{session_id}:{seq}",
        "observations": observations}, ensure_ascii=False), "utf-8")
    os.replace(tmp, OBS_PATH)
    print(f"[obs] {section_key} -> {observations}")


def archive_affect_sample(owner, section, captured_at, people, frame=None, error=None):
    """Keep capture-time identity/section even when an inference finishes in a later session."""
    sample_id = uuid.uuid4().hex
    evidence = []
    if frame is not None:
        folder = RECORDS_DIR / "evidence"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{owner.session_id}_{time.time_ns()}_0.jpg"
        if cv2.imwrite(str(folder / filename), frame):
            evidence.append(filename)
    people = [dict(p) for p in people or []]
    names = [p.get("name") for p in people]
    for p in people:
        if p.get("name") not in owner.names or names.count(p.get("name")) > 1:
            p["name"] = "?"
        affect = p.get("affect", "?")
        p["affect"] = affect if affect in PGC_AFFECTS else "?"
        p["mood"] = PGC_AFFECTS[affect][0] if affect in PGC_AFFECTS else "?"
    return append_event(owner.session_id, "affect", {
        "sample_id": sample_id, "observation_id": f"{owner.session_id}:affect:{sample_id}",
        "section": section, "captured_at": captured_at, "model": PREVIEW_MODEL,
        "status": error or ("recognized" if people else "no_people"),
        "people": people, "evidence": evidence}, directory=RECORDS_DIR)


# ---------------- 主循环 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="视觉互动教练(独立进程)")
    ap.add_argument("--cam", type=int, default=None,
                    help="用本机摄像头编号(默认走机器人外接Brio)")
    ap.add_argument("--standalone", action="store_true",
                    help="不读时钟文件,回车手动对齐 demo 开始")
    ap.add_argument("--show", action="store_true", help="显示调试窗口")
    ap.add_argument("--web", action="store_true",
                    help="打开浏览器控制台(实时画面/参与者/启动演出程序)")
    ap.add_argument("--port", type=int, default=None, help="--web 端口,默认 8765")
    args = ap.parse_args()

    grabber = FrameGrabber(args)
    grabber.start()
    clock = Clock(args.standalone)
    director = Director()
    if args.standalone:
        clock.start_standalone()

    seq = 0
    cur_section = None
    keyframes: list = []
    capture_times: list = []
    session_lock = threading.RLock()
    kf_idx = 0
    judging = False
    status = "開始待ち…"
    preview = {"people": [], "wall": 0.0, "busy": False, "last_try": 0.0}
    alert = {"names": [], "wall": 0.0}   # 最近一次评判里没在动的人(画面红字提示)
    ALERT_SECONDS = 8.0
    last_judge: dict = {}                # 最近一次评判(网页参与者卡片用)
    events: list = []                    # 网页事件日志(最新在后,只留 80 条)

    def log_event(kind: str, text: str) -> None:
        events.append({"t": time.time(), "kind": kind, "text": text})
        del events[:-80]

    def judge_async(section_key, frames, my_seq, timestamps, owner):
        nonlocal judging
        try:
            if len(frames) < len(rules.SECTIONS[section_key]["keyframe_beats"]):
                results, error = [], "insufficient_frames"
            else:
                results = vlm_judge(section_key, frames)
                error = None if results is not None else "recognition_failed"
        except Exception as exc:
            results, error = [], type(exc).__name__
        with session_lock:
            if my_seq != seq or owner is not director:
                return
            observation_id = f"{owner.session_id}:{my_seq}"
            evidence = []
            evidence_dir = RECORDS_DIR / "evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            for i, frame in enumerate(frames):
                filename = f"{owner.session_id}_{my_seq}_{i}.jpg"
                if cv2.imwrite(str(evidence_dir / filename), frame):
                    evidence.append(filename)
            owner.audit[section_key] = {
                "observation_id": observation_id, "captured_at": timestamps,
                "evidence": evidence,
                "judged_at": time.strftime("%H:%M:%S"),
                "status": error or ("recognized" if results else "no_identified_people")}
            owner.history = [entry for entry in owner.history if entry[0] != section_key]
            obs = owner.decide(section_key, results or [])
            for i, feedback in enumerate(obs):
                target = next((r for r in results or [] if r["name"] == feedback["name"]), {})
                feedback.update(feedback_id=f"{observation_id}:{i}",
                                kind="still" if target.get("moving") is False else
                                "issue" if target.get("issue") else
                                "encourage" if target.get("energy") == "low" else "praise")
            owner._write_report(sorted({r["name"] for _, rs, _ in owner.history for r in rs}))
            still = [r["name"] for r in results or [] if r.get("moving") is False]
            alert.update(names=still, wall=time.time())
            last_judge.clear()
            last_judge.update(section=section_key, results=results or [], obs=obs,
                              wall=time.time(), observation_id=observation_id,
                              session_id=owner.session_id, captured_at=timestamps,
                              status=owner.audit[section_key]["status"])
            log_event("warn" if error else "judge", f"{section_key}: {owner.audit[section_key]['status']}")
            for o in obs:
                log_event("info", f"[予定 / planned] {o.get('name', '')}さん、{o.get('advice', '')}")
            write_obs(section_key, my_seq, obs, owner.session_id)
            judging = False

    affect_log: list = []                # 每次认出 A/B 且看清表情就记一条(网页表情日志)
    affect_counts = {}

    def preview_async(frame, captured_at, owner, section, in_session):
        try:
            people = vlm_locate(frame)
            error = "recognition_failed" if people is None else None
        except Exception as exc:
            error = type(exc).__name__
            people = []
        people = people or []
        sample = None
        if in_session:
            try:
                sample = archive_affect_sample(owner, section, captured_at, people, frame, error)
                people = sample["people"]
            except Exception as exc:
                log_event("warn", f"表情記録の保存失敗: {exc}")
        if owner is not director:
            preview.update(busy=False)
            return
        # Duplicate labels cannot identify a unique person.
        names = [p["name"] for p in people]
        for p in people:
            if names.count(p["name"]) > 1:
                p["name"] = "?"
        for p in people:
            if in_session and sample and p["name"] in rules.NAMES:
                counts = affect_counts.setdefault(p["name"], {"pos": 0, "neg": 0, "unknown": 0})
                counts["pos" if p["mood"] == "积极" else "neg" if p["mood"] == "消极" else "unknown"] += 1
                affect_log.append({"t": captured_at, "name": p["name"],
                                   "affect": p["affect"], "mood": p["mood"],
                                   "action": p.get("action", ""),
                                   "session_id": owner.session_id if in_session else None,
                                   "section": section,
                                   "sample_id": sample["sample_id"] if sample else None})
        del affect_log[:-500]
        preview.update(people=people, wall=captured_at, busy=False, frame=frame)

    print(f"[run] 模型={VLM_MODEL} 评判节={list(rules.SECTIONS)}")
    win = "vision_coach (q quit)"
    if args.show:
        # 可缩放窗口:画面随窗口拉伸/最大化,保持 16:9 比例
        cv2.namedWindow(win, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        cv2.resizeWindow(win, 1280, 720)
    render = args.show or args.web
    live = {"key": None, "beat": None, "frame_wall": 0.0}
    web_frame = None
    if args.web:
        import vision_web
        speech_cache = {"session_id": None, "at": 0.0, "events": []}

        def web_state() -> dict:
            now = time.time()
            if speech_cache["session_id"] != director.session_id or now - speech_cache["at"] > 1:
                try:
                    receipts = [e for e in read_events(director.session_id, RECORDS_DIR)
                                if e["event_type"] == "speech"]
                    speech_cache.update(session_id=director.session_id, at=now, events=receipts[-80:])
                except (OSError, ValueError) as exc:
                    log_event("warn", f"音声記録を読み込めません: {exc}")
            fresh = not clock.active and now - preview["wall"] < 4 * PREVIEW_INTERVAL
            return {
                "now": now,
                "status": status,
                "clock_active": clock.active,
                "section": live["key"],
                "beat": live["beat"],
                "camera_ok": now - grabber.captured_at < 2.0,
                "session_id": director.session_id,
                "frame_mode": "snapshot" if fresh and preview.get("frame") is not None else "live",
                "frame_captured_at": preview["wall"] if fresh else grabber.captured_at,
                "judging": judging,
                "people": preview["people"] if fresh else [],
                "people_age": now - preview["wall"] if preview["wall"] else None,
                "alert": alert["names"] if now - alert["wall"] < ALERT_SECONDS else [],
                "last_judge": last_judge,
                "events": events[-40:],
                "speech_events": speech_cache["events"],
                "affect_log": affect_log[-150:],
                "affect_stats": {n: dict(affect_counts.get(n, {"pos": 0, "neg": 0, "unknown": 0}))
                                 for n in rules.NAMES},
                "names": list(rules.NAMES),
                "nicknames": dict(rules.NICKNAMES),
                "added_at": dict(rules.ADDED_AT),
                "max_participants": rules.MAX_PARTICIPANTS,
                "sections": [{"key": k, "title": s["title"]}
                             for k, s in rules.SECTIONS.items()],
                "models": {"judge": VLM_MODEL, "preview": PREVIEW_MODEL},
                "still_diff": STILL_DIFF,
                "affects": {k: {"mood": v[0], "ja": v[2], "emoji": AFFECT_EMOJI[k]}
                            for k, v in PGC_AFFECTS.items()},
                "actions_ja": ACTION_JA,
            }

        def set_participants(opts: dict) -> dict:
            if clock.active or judging:
                raise ValueError("演出或评判进行中，不能修改参与者名单")
            before = list(rules.NAMES)
            rules.set_participants(opts.get("participants") or [])
            print(f"[web] 参与者更新: {before} -> {rules.NAMES}")
            log_event("info", "参加者: " + ", ".join(
                f"{n}({rules.NICKNAMES[n]})" if rules.NICKNAMES.get(n) else n
                for n in rules.NAMES))
            return {"names": list(rules.NAMES), "nicknames": dict(rules.NICKNAMES),
                    "added_at": dict(rules.ADDED_AT)}

        web_frame, _launcher = vision_web.serve(
            web_state, port=args.port or vision_web.DEFAULT_PORT,
            actions={"/api/participants": set_participants})
        log_event("info", "コンソール起動")
    while True:
        key, beat = clock.now()
        with session_lock:
            if clock.active and clock.session_id and clock.session_id != director.session_id:
                seq += 1
                director = Director(clock.session_id)
                director._write_report([])
                cur_section = None
                keyframes, capture_times, kf_idx = [], [], 0
                last_judge.clear()
                affect_log.clear()
                affect_counts.clear()
                preview.update(people=[], wall=0.0, frame=None)
                alert.update(names=[], wall=0.0)
                judging = False
                log_event("info", "Session: " + director.session_id)
        live.update(key=key, beat=beat)
        if key != cur_section:
            cur_section = key
            keyframes, capture_times, kf_idx = [], [], 0
            if key:
                log_event("section", f"{key} 開始")
        if key in rules.SECTIONS and beat is not None:
            beats = rules.SECTIONS[key]["keyframe_beats"]
            if kf_idx < len(beats) and beat >= beats[kf_idx]:
                frame, captured_at = grabber.sample()
                if frame is not None and captured_at not in capture_times:
                    keyframes.append(frame)
                    capture_times.append(captured_at)
                kf_idx += 1
                status = f"{key} {beat:.1f}拍 撮影{len(keyframes)}/{len(beats)}"
                if kf_idx == len(beats):
                    judging = True         # 不做互斥:新评判直接发起,旧的作废
                    seq += 1
                    threading.Thread(
                        target=judge_async, args=(key, list(keyframes), seq, list(capture_times), director),
                        daemon=True).start()
                    status = f"{key} 判定中…"
        elif key is None:
            status = "開始待ち…" if not args.standalone else "待機中"

        # Session sampling also runs without --web/--show; judgments take priority.
        now = time.time()
        if (clock.active or render) and not judging and not preview["busy"] and \
                now - preview["last_try"] > PREVIEW_INTERVAL:
            frame, captured_at = grabber.sample()
            preview["last_try"] = now
            if frame is not None:
                preview.update(busy=True)
                threading.Thread(target=preview_async,
                                 args=(frame, captured_at, director, key, clock.active),
                                 daemon=True).start()
            elif clock.active:
                try:
                    archive_affect_sample(director, key, now, [], error="camera_unavailable")
                except Exception as exc:
                    log_event("warn", f"表情記録の保存失敗: {exc}")
        if render:
            img = grabber.latest()
            if img is not None:
                live["frame_wall"] = grabber.captured_at
                snapshot = dict(preview)
                annotated = (not clock.active and snapshot.get("frame") is not None
                             and now - snapshot["wall"] < 4 * PREVIEW_INTERVAL)
                if annotated:
                    img = snapshot["frame"]
                disp = cv2.resize(img, (960, 540))
                if annotated:
                    for p in snapshot["people"]:
                        _draw_person(disp, p)
                if web_frame is not None:
                    # 网页:画面只带人物框/emoji,状态和提示由页面本身显示
                    web_frame.publish(disp)
                if not args.show:
                    time.sleep(0.03)
                    continue
                if now - preview["wall"] < 4 * PREVIEW_INTERVAL:
                    found = ",".join(p["name"] for p in preview["people"]) or "なし"
                    cv2.putText(disp, f"検出: {found}", (10, 65),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                cv2.putText(disp, status, (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                if alert["names"] and now - alert["wall"] < ALERT_SECONDS:
                    # 没在动的人:画面顶部居中红底白字,持续 ALERT_SECONDS 秒
                    msg = f"⚠ {','.join(alert['names'])}: 動いていません"
                    disp[90:140, 180:780] = (disp[90:140, 180:780] * 0.3
                                             + np.array([0, 0, 200]) * 0.7
                                             ).astype(disp.dtype)
                    cv2.putText(disp, msg, (200, 127),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
                cv2.imshow(win, disp)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    return 0
        time.sleep(0.03)


if __name__ == "__main__":
    sys.exit(main())
