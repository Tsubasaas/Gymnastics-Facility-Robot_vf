# -*- coding: utf-8 -*-
"""
千问语音合成（Qwen3-TTS-Flash，阿里云国际版）→ Windows 本地播放。

给 ai_viewer 提供"喊操员"旁白：动作开始报动作名、广播体操逐节报节名。
- speak(text)：异步排队播报，不卡渲染循环；同一文本合成结果按内容缓存，
  第二次播放不再调 API。
- prefetch(texts)：后台预热一批文本（如13节节名），播放时零等待。
- set_enabled(flag)：静音开关。

播放实现：wav 用内置 winsound；其他格式（mp3等）走 winmm MCI，都无需额外依赖。
真机模式可用 set_robot_sink(robot_speaker.RobotSpeaker()) 把声音切到 G1 扬声器，
机器人端播放失败会自动回退本机。
"""
import ctypes
import hashlib
import os
import queue
import tempfile
import threading
import time
import uuid
import urllib.request

import dashscope

from llm_motion import API_KEY

dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"   # 国际版
TTS_MODEL = os.environ.get("QWEN_TTS_MODEL", "qwen3-tts-flash")   # 千问最新TTS
VOICE = os.environ.get("QWEN_TTS_VOICE", "Cherry")
LANGUAGE_TYPE = os.environ.get("QWEN_TTS_LANGUAGE", "Japanese")

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "g1_tts_cache")
os.makedirs(_CACHE_DIR, exist_ok=True)

_q = queue.Queue()
_enabled = True
_lock = threading.Lock()
_worker = None
_robot_sink = None   # 非None时优先从 G1 扬声器播出（见 set_robot_sink）
_playing = False     # 当前是否有一条正在合成/播放（供 is_busy 查询）


def is_busy():
    """是否正在播报或有排队未播——节拍报数用它实现"忙则跳过不排队"。"""
    return _playing or not _q.empty()


def set_robot_sink(sink):
    """把播放出口切到 G1 扬声器（sink=robot_speaker.RobotSpeaker 实例）。

    传 None 恢复本机播放。机器人端播放异常时单条自动回退本机，不中断队列。
    """
    global _robot_sink
    _robot_sink = sink


def _api_key():
    return os.environ.get("DASHSCOPE_API_KEY", "") or API_KEY


def synth(text):
    """文本 → 本地音频文件路径（按内容缓存）。失败抛 RuntimeError。"""
    path = os.path.join(
        _CACHE_DIR,
        hashlib.md5(
            f"{TTS_MODEL}|{VOICE}|{LANGUAGE_TYPE}|{text}".encode("utf-8")
        ).hexdigest() + ".audio")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    resp = dashscope.MultiModalConversation.call(
        model=TTS_MODEL, api_key=_api_key(), text=text,
        voice=VOICE, language_type=LANGUAGE_TYPE, stream=False)
    if getattr(resp, "status_code", None) != 200:
        raise RuntimeError(f"TTS调用失败: {getattr(resp, 'code', '')} "
                           f"{getattr(resp, 'message', resp)}")
    url = resp.output.audio["url"]
    urllib.request.urlretrieve(url, path)
    return path


def _play_blocking(path):
    with open(path, "rb") as f:
        is_wav = f.read(4) == b"RIFF"
    if is_wav:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
    else:   # mp3 等其他格式走 MCI
        alias = f"g1tts{abs(hash(path)) % 100000}"
        mci = ctypes.windll.winmm.mciSendStringW
        code = mci(f'open "{path}" alias {alias}', None, 0, None)
        if code:
            raise RuntimeError(f"MCI open failed: {code}")
        try:
            code = mci(f"play {alias} wait", None, 0, None)
            if code:
                raise RuntimeError(f"MCI playback failed: {code}")
        finally:
            mci(f"close {alias}", None, 0, None)


def _emit(job, status, **details):
    callback = job.get("on_event")
    if callback:
        try:
            callback({**job["context"], **details, "utterance_id": job["id"],
                      "text": job["text"], "status": status, "at": time.time()})
        except Exception as exc:
            print(f"[audio receipt write failed] {exc}")


def _process(job):
    current = job.get("is_current") or (lambda: True)
    if not _enabled or not current():
        _emit(job, "skipped", reason="muted" if not _enabled else "session_changed")
        return
    _emit(job, "synthesizing")
    try:
        path = synth(job["text"])
    except Exception as exc:
        _emit(job, "failed", stage="synthesis", error=str(exc))
        return
    if not _enabled or not current():
        _emit(job, "skipped", reason="muted" if not _enabled else "session_changed")
        return
    sink = _robot_sink
    if sink is not None:
        _emit(job, "playing", output="robot")
        try:
            result = sink.play_file(path)
            # PlayStream acknowledges transport, not physical playback completion.
            details = result if isinstance(result, dict) else {}
            _emit(job, details.pop("status", "robot_sent"), output="robot", **details)
            return
        except Exception as exc:
            _emit(job, "fallback", output="robot", error=str(exc))
    if not _enabled or not current():
        _emit(job, "skipped", reason="muted" if not _enabled else "session_changed")
        return
    _emit(job, "playing", output="local")
    try:
        _play_blocking(path)
        _emit(job, "completed", output="local", confirmation="blocking_player_returned")
    except Exception as exc:
        _emit(job, "failed", stage="playback", output="local", error=str(exc))


def _run():
    global _playing
    while True:
        job = _q.get()
        _playing = True
        try:
            _process(job)
        except Exception as exc:
            _emit(job, "failed", stage="worker", error=str(exc))
        finally:
            _playing = False
            _q.task_done()


def speak(text, *, context=None, on_event=None, is_current=None):
    """异步播报。多条按顺序排队，不打断也不重叠。"""
    global _worker
    job = {"id": uuid.uuid4().hex, "text": str(text or "").strip(),
           "context": dict(context or {}), "on_event": on_event, "is_current": is_current}
    if not _enabled or not job["text"]:
        _emit(job, "skipped", reason="muted" if not _enabled else "empty_text")
        return job["id"]
    with _lock:
        _emit(job, "queued")
        _q.put(job)
        if _worker is None:
            _worker = threading.Thread(target=_run, daemon=True)
            _worker.start()
    return job["id"]


def prefetch(texts):
    """后台预合成一批文本（只缓存不播放），播放时零延迟。"""
    def job():
        for t in texts:
            try:
                synth(str(t).strip())
            except Exception:
                pass
    threading.Thread(target=job, daemon=True).start()


def set_enabled(flag):
    global _enabled
    _enabled = bool(flag)
    if not flag:
        while not _q.empty():   # 清空未播的排队项
            try:
                job = _q.get_nowait()
                _emit(job, "skipped", reason="muted")
                _q.task_done()
            except queue.Empty:
                break
    return _enabled


def is_enabled():
    return _enabled
