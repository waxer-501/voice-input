#!/usr/bin/env python3.10
"""Push-to-talk voice input via ByteDance Volcano streaming ASR.

Hold Ctrl+Win to record; release to paste recognized text at cursor.
Audio is streamed to ASR in real-time during recording, so results are
ready almost instantly on release. Text is pasted via shift+Insert
(which most terminals inject as keyboard input, bypassing app-level
clipboard parsing that can misinterpret image targets). Falls back to
xdotool type if clipboard operations fail.

Config: .env in same dir (VOLC_APPID, VOLC_TOKEN, VOLC_RESOURCE).
Run:     python3.10 voice_input.py
"""

import os
import sys
import math
import json
import uuid
import struct
import wave
import asyncio
import threading
import time
import subprocess
from pathlib import Path

# ---------- env ----------
_SCRIPT_DIR = Path(__file__).resolve().parent
_envfile = _SCRIPT_DIR / ".env"
if _envfile.is_file():
    for _line in _envfile.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

APPID = os.environ.get("VOLC_APPID", "")
TOKEN = os.environ.get("VOLC_TOKEN", "")
RESOURCE = os.environ.get("VOLC_RESOURCE", "volc.seedasr.sauc.duration")
ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"

HOTKEY = {"ctrl", "win"}
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200
FINAL_WAIT_SEC = 3
PASTE_KEY = "shift+Insert"

CACHE_DIR = os.path.expanduser("~/.cache/voiceinput")
BEEP_START = os.path.join(CACHE_DIR, "beep_start.wav")
BEEP_STOP = os.path.join(CACHE_DIR, "beep_stop.wav")
BEEP_ERR = os.path.join(CACHE_DIR, "beep_err.wav")

_state = {"held": set(), "recording": False}
_lock = threading.Lock()
_loop = None
_session = None


# ---------- beeps ----------
def ensure_beeps():
    os.makedirs(CACHE_DIR, exist_ok=True)

    def gen(path, tones):
        sr = 16000
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            for freq, dur in tones:
                n = int(sr * dur)
                attack = int(sr * 0.005)
                frames = bytearray()
                for i in range(n):
                    env = min(1.0, i / max(1, attack)) * min(1.0, (n - i) / max(1, attack))
                    val = int(32767 * 0.4 * env * math.sin(2 * math.pi * freq * i / sr))
                    frames += struct.pack("<h", max(-32768, min(32767, val)))
                w.writeframes(frames)

    if not os.path.exists(BEEP_START):
        gen(BEEP_START, [(880, 0.09)])
    if not os.path.exists(BEEP_STOP):
        gen(BEEP_STOP, [(660, 0.07), (440, 0.09)])
    if not os.path.exists(BEEP_ERR):
        gen(BEEP_ERR, [(300, 0.08), (220, 0.12)])


def play(path):
    try:
        subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------- volcano ASR protocol ----------
def pack_first(payload: bytes) -> bytes:
    return struct.pack(">BBBB", 0x11, 0x10, 0x10, 0x00) + struct.pack(">I", len(payload)) + payload


def pack_audio(audio: bytes, final: bool = False) -> bytes:
    flags = 0b0010 if final else 0b0000
    return struct.pack(">BBBB", 0x11, (0b0010 << 4) | flags, 0x00, 0x00) + struct.pack(">I", len(audio)) + audio


def parse_response(data: bytes):
    if len(data) < 4:
        return None, None, False
    msg_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    is_last = (flags & 0b0010) != 0
    idx = data.find(b"{", 4)
    if idx < 0:
        if msg_type == 0b1111:
            return "err", data[4:].decode("utf-8", "ignore"), is_last
        return None, None, is_last
    payload = data[idx:]
    if msg_type == 0b1111:
        return "err", payload.decode("utf-8", "ignore"), is_last
    try:
        obj = json.loads(payload.decode("utf-8"))
    except Exception:
        return None, None, is_last
    return "asr", obj, is_last


def extract_text(obj: dict) -> str:
    r = obj.get("result")
    if isinstance(r, dict):
        return r.get("text", "")
    if isinstance(r, list):
        return "".join((x.get("text", "") if isinstance(x, dict) else str(x)) for x in r)
    if isinstance(r, str):
        return r
    return ""


# ---------- text aggregation ----------
class TextAggregator:
    """Accumulate ASR text across VAD utterance boundaries."""

    def __init__(self):
        self.final_text = ""
        self.last_text = ""

    def update(self, text: str, is_last: bool):
        if text:
            self.last_text = text
        if is_last and text:
            if not self.final_text:
                self.final_text = text
            elif text.startswith(self.final_text):
                self.final_text = text
            elif self.final_text.startswith(text):
                pass
            else:
                self.final_text += text

    def best(self) -> str:
        return (self.final_text or self.last_text).strip()


# ---------- streaming session ----------
class Session:
    """One ASR session: arecord -> WebSocket -> text, all in real-time."""

    def __init__(self, target_wid):
        self.target_wid = target_wid
        self.arecord = None
        self.ws = None
        self.stop_event = asyncio.Event()
        self.aggregator = TextAggregator()
        self.done = asyncio.Event()
        self.error = None

    async def run(self):
        try:
            await self._run()
        except Exception as e:
            self.error = e
            sys.stderr.write("session error: %s\n" % e)
        finally:
            self.done.set()

    async def _run(self):
        import websockets

        headers = {
            "X-Api-App-Key": APPID,
            "X-Api-Access-Key": TOKEN,
            "X-Api-Resource-Id": RESOURCE,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        cfg = {
            "user": {"uid": "voice_input"},
            "audio": {"format": "pcm", "rate": SAMPLE_RATE, "bits": 16, "channel": 1, "codec": "raw"},
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": False,
                "enable_nonstream": True,
                "result_type": "full",
                "show_utterances": True,
            },
        }

        self.arecord = subprocess.Popen(
            ["arecord", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        async with websockets.connect(
            ENDPOINT, additional_headers=headers,
            open_timeout=20, ping_interval=None, max_size=10_000_000,
        ) as ws:
            self.ws = ws
            await ws.send(pack_first(json.dumps(cfg, ensure_ascii=False).encode()))

            async def sender():
                loop = asyncio.get_event_loop()
                try:
                    while not self.stop_event.is_set():
                        chunk = await loop.run_in_executor(
                            None, self.arecord.stdout.read, CHUNK_BYTES)
                        if not chunk:
                            break
                        await ws.send(pack_audio(chunk))
                    await ws.send(pack_audio(b"", final=True))
                except Exception as e:
                    sys.stderr.write("sender error: %s\n" % e)
                finally:
                    if self.arecord.returncode is None:
                        self.arecord.terminate()

            async def receiver():
                try:
                    while True:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        if not isinstance(msg, (bytes, bytearray)):
                            continue
                        tag, data, is_last = parse_response(bytes(msg))
                        if tag == "err":
                            sys.stderr.write("[ASR error] %s\n" % data)
                            return
                        if tag == "asr":
                            self.aggregator.update(extract_text(data), is_last)
                            if is_last:
                                return
                except asyncio.TimeoutError:
                    sys.stderr.write("receiver timeout\n")
                except Exception as e:
                    sys.stderr.write("receiver error: %s\n" % e)

            await asyncio.gather(sender(), receiver())

    def stop_and_get_text(self) -> str:
        """Signal stop, kill arecord immediately, best-effort wait for ASR."""
        _loop.call_soon_threadsafe(self.stop_event.set)
        # Kill arecord immediately so recording stops the instant the
        # user releases the hotkey, regardless of ASR round-trip latency.
        if self.arecord and self.arecord.returncode is None:
            self.arecord.terminate()
            try:
                self.arecord.wait(timeout=2)
            except Exception:
                self.arecord.kill()
        try:
            self.arecord.stdout.close()
        except Exception:
            pass
        # Best-effort wait for ASR to return final text.
        future = asyncio.run_coroutine_threadsafe(self.done.wait(), _loop)
        try:
            future.result(timeout=FINAL_WAIT_SEC)
        except Exception:
            pass
        return self.aggregator.best()


# ---------- clipboard paste ----------
def paste_text(text: str, target_wid):
    # Save old selections so we can restore after paste.
    old_clipboard = b""
    old_primary = b""
    for selection, holder in (("clipboard", "old_clipboard"),
                              ("primary", "old_primary")):
        try:
            data = subprocess.run(
                ["xclip", "-selection", selection, "-o"],
                capture_output=True, timeout=2,
            ).stdout
            if selection == "clipboard":
                old_clipboard = data
            else:
                old_primary = data
        except Exception:
            pass

    # Write text to both selections: shift+Insert typically uses PRIMARY,
    # but some terminals read CLIPBOARD, so populate both.
    wrote_ok = False
    for selection in ("primary", "clipboard"):
        try:
            subprocess.run(
                ["xclip", "-selection", selection],
                input=text.encode("utf-8"), timeout=2, check=True,
            )
            wrote_ok = True
        except Exception:
            pass

    if not wrote_ok:
        # xclip unavailable or failed — type directly via xdotool.
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "1", text],
            check=False, timeout=10,
        )
        return

    if target_wid:
        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", str(target_wid)],
                check=False, timeout=1,
            )
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", PASTE_KEY],
            check=False, timeout=3,
        )
    except Exception:
        # shift+Inject via xdotool failed — fall back to typing.
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "1", text],
            check=False, timeout=10,
        )

    # Restore old selections.
    time.sleep(0.15)
    for selection, old in (("clipboard", old_clipboard),
                           ("primary", old_primary)):
        try:
            subprocess.run(
                ["xclip", "-selection", selection],
                input=old, timeout=2,
            )
        except Exception:
            pass


# ---------- hotkey ----------
def get_active_window():
    try:
        r = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=0.5,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def normalize(key):
    name = None
    if hasattr(key, "name"):
        name = key.name
    elif hasattr(key, "char") and key.char:
        name = key.char
    if not isinstance(name, str):
        return None
    n = name.lower()
    if n in ("ctrl", "ctrl_l", "ctrl_r"):
        return "ctrl"
    if n in ("cmd", "cmd_l", "cmd_r", "super", "super_l", "super_r",
             "meta", "win", "windows"):
        return "win"
    return None


def on_press(key):
    k = normalize(key)
    if k is None:
        return
    _state["held"].add(k)
    with _lock:
        if not HOTKEY.issubset(_state["held"]):
            return
        if _state["recording"]:
            return
        global _session
        # Kill any lingering arecord from a previous session whose
        # stop_and_get_text is still waiting for ASR results.
        if _session is not None and _session.arecord and _session.arecord.returncode is None:
            _session.arecord.terminate()
        _state["recording"] = True
        wid = get_active_window()
        _session = Session(wid)
        asyncio.run_coroutine_threadsafe(_session.run(), _loop)
        play(BEEP_START)
        print("[recording started]", flush=True)


def on_release(key):
    k = normalize(key)
    if k is None:
        return
    _state["held"].discard(k)
    with _lock:
        if _state["recording"] and not HOTKEY.issubset(_state["held"]):
            _state["recording"] = False
            session = _session
            threading.Thread(target=_finish, args=(session,), daemon=True).start()


def _finish(session):
    if session is None:
        return
    text = session.stop_and_get_text()
    play(BEEP_STOP)
    print("recognized: %s" % text, flush=True)
    if text:
        paste_text(text, session.target_wid)
    else:
        play(BEEP_ERR)


# ---------- main ----------
def main():
    if not APPID or not TOKEN:
        sys.exit("missing VOLC_APPID / VOLC_TOKEN in .env or env")
    ensure_beeps()

    global _loop
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True).start()

    from pynput import keyboard
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    print("ready: hold Ctrl+Win to speak, release to paste. Ctrl+C to quit.", flush=True)
    while True:
        try:
            if not listener.is_alive():
                print("listener died, restarting...", flush=True)
                listener = keyboard.Listener(on_press=on_press, on_release=on_release)
                listener.start()
            time.sleep(1)
        except KeyboardInterrupt:
            print("\nExiting...")
            _loop.call_soon_threadsafe(_loop.stop)
            return


if __name__ == "__main__":
    main()
