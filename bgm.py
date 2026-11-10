# -*- coding: utf-8 -*-
"""演出 BGM:真机动作开始时从电脑扬声器播放,结束/中断时淡出停止。

走 Windows winmm MCI(与 tts_qwen 播 mp3 同一套),无需额外依赖;
TTS 语音用另一个 MCI 别名,两路声音由系统混音、互不打断。

音频文件:默认项目目录下的 bgm.mp3(已 .gitignore,不入库)。
MCI 解不了 mp4,原始视频需先转 mp3,例如:
    ffmpeg -ss 4.8 -i 原视频.mp4 -vn -b:a 192k bgm.mp3
环境变量:
    BGM_PATH    换一个音频文件
    BGM_VOLUME  0~1000,默认 400(给语音留出空间)
    BGM=0       关闭 BGM
"""

import ctypes
import os
import threading
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
BGM_PATH = os.environ.get("BGM_PATH") or str(PROJECT_DIR / "bgm.mp3")
VOLUME = max(0, min(1000, int(os.environ.get("BGM_VOLUME", "400"))))
ENABLED = os.environ.get("BGM", "1") != "0"

_ALIAS = "g1bgm"
_lock = threading.Lock()
_playing = False


def _mci(cmd: str) -> int:
    return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)


def available() -> bool:
    return ENABLED and os.path.isfile(BGM_PATH)


def start() -> None:
    """从头开始播放(非阻塞)。文件不存在或已关闭时静默跳过。"""
    global _playing
    if not available():
        return
    with _lock:
        if _playing:
            _mci(f"close {_ALIAS}")
        if _mci(f'open "{BGM_PATH}" type mpegvideo alias {_ALIAS}') != 0:
            print(f"⚠ BGM 打开失败: {BGM_PATH}")
            _playing = False
            return
        _mci(f"setaudio {_ALIAS} volume to {VOLUME}")
        _mci(f"play {_ALIAS} from 0")
        _playing = True
    print(f"♪ BGM 开始播放(音量 {VOLUME}/1000)")


def stop(fade_s: float = 1.5) -> None:
    """淡出后停止并释放设备;未在播放时什么都不做。阻塞约 fade_s 秒。"""
    global _playing
    with _lock:
        if not _playing:
            return
        steps = max(1, int(fade_s / 0.05))
        for i in range(steps, -1, -1):
            _mci(f"setaudio {_ALIAS} volume to {VOLUME * i // steps}")
            time.sleep(fade_s / steps)
        _mci(f"stop {_ALIAS}")
        _mci(f"close {_ALIAS}")
        _playing = False
