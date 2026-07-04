#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "websockets>=13",
#   "sounddevice>=0.5",
# ]
# ///
"""火山引擎流式 ASR + 持按通话 IME（一文件版）。

凭据来源：同目录 .env 或环境变量
    VOLC_APPID, VOLC_TOKEN, VOLC_RESOURCE   # 例: volc.seedasr.sauc.duration

模式：
    talky.py file <wav>     16kHz/16bit/mono WAV 转写（实时增量到 stdout）
    talky.py mic  <sec>     录 N 秒并转写
    talky.py daemon         录到收到 SIGTERM/SIGINT；进度→stderr，终稿→stdout
    talky.py start          后台启动 IME 守护，立即返回（i3 keypress 调）
    talky.py stop           触发守护停录，等终稿，xdotool 注入（i3 keyrelease 调）
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import struct
import sys
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import websockets


# ---------- env loading ----------

def _load_env() -> None:
    envfile = Path(__file__).resolve().parent / ".env"
    if not envfile.is_file():
        return
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


# ---------- constants ----------

ENDPOINT = os.environ.get(
    "VOLC_ENDPOINT",
    "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
)

SAMPLE_RATE   = 16000
CHUNK_MS      = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000      # 1600 samples per chunk

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
SOCK_PATH   = RUNTIME_DIR / "talky.sock"
PID_PATH    = RUNTIME_DIR / "talky.pid"
LOG_PATH    = RUNTIME_DIR / "talky.log"

APPID = TOKEN = RESOURCE = ""


# ---------- byte protocol ----------

# byte0 = protocol_version<<4 | header_size (always 0x11).
# byte1 = message_type<<4 | flags. flags: 0b0000 normal, 0b0001 +seq,
#         0b0010 last, 0b0011 last+neg-seq.
# byte2 = serialization<<4 | compression. byte3 = reserved.
HDR_FULL_CLIENT = bytes([0x11, 0x10, 0x11, 0x00])   # type=1 JSON+gzip
HDR_AUDIO       = bytes([0x11, 0x20, 0x01, 0x00])   # type=2 raw+gzip
HDR_AUDIO_LAST  = bytes([0x11, 0x22, 0x01, 0x00])   # type=2 last raw+gzip


def frame(header: bytes, payload: bytes) -> bytes:
    payload = gzip.compress(payload)
    return header + struct.pack(">I", len(payload)) + payload


def config_payload() -> bytes:
    return json.dumps({
        "user":  {"uid": "talky-linux", "platform": "Linux"},
        "audio": {"format": "pcm", "codec": "raw",
                  "rate": SAMPLE_RATE, "bits": 16, "channel": 1},
        "request": {
            "model_name":      "bigmodel",
            "enable_itn":      True,
            "enable_punc":     True,
            "enable_ddc":      False,
            "show_utterances": True,
            "result_type":     "single",
        },
    }, ensure_ascii=False).encode()


def parse(data: bytes | str) -> dict:
    if isinstance(data, str):
        data = data.encode("latin-1")
    msg_type   = data[1] >> 4
    flags      = data[1] & 0x0F
    compressed = (data[2] & 0x0F) == 0x01
    off = 4
    if msg_type == 0x0F:
        code = struct.unpack(">I", data[off:off + 4])[0]; off += 4
        size = struct.unpack(">I", data[off:off + 4])[0]; off += 4
        body = data[off:off + size]
        if compressed or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return {"_error": code, "message": body.decode("utf-8", "replace")}
    seq = None
    if flags & 0x01:
        seq = struct.unpack(">i", data[off:off + 4])[0]; off += 4
    size = struct.unpack(">I", data[off:off + 4])[0]; off += 4
    body = data[off:off + size]
    if compressed:
        body = gzip.decompress(body)
    obj = json.loads(body) if body else {}
    obj["_seq"]   = seq
    obj["_flags"] = flags
    return obj


# ---------- audio sources ----------

async def file_chunks(path: str) -> AsyncIterator[bytes]:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError(
                f"need 16kHz/mono/16bit WAV, got "
                f"{w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth()*8}bit"
            )
        while (chunk := w.readframes(CHUNK_SAMPLES)):
            yield chunk
            await asyncio.sleep(CHUNK_MS / 1000)


async def mic_chunks(seconds: float | None = None,
                     stop: asyncio.Event | None = None) -> AsyncIterator[bytes]:
    """Yield 16-bit PCM chunks from default input device. Stops on whichever
    happens first: deadline reached (if `seconds` set) or `stop` event set."""
    import sounddevice as sd
    q: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def cb(indata, _frames, _t, status):
        if status:
            print(status, file=sys.stderr)
        loop.call_soon_threadsafe(q.put_nowait, bytes(indata))

    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=CHUNK_SAMPLES,
                           channels=1, dtype="int16", callback=cb):
        deadline = loop.time() + seconds if seconds is not None else None
        while True:
            if stop is not None and stop.is_set():
                return
            timeout = 0.1
            if deadline is not None:
                remain = deadline - loop.time()
                if remain <= 0:
                    return
                timeout = min(timeout, remain)
            try:
                yield await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                continue


# ---------- streaming core ----------

class _NullSink:
    def write(self, _s: str) -> int: return 0
    def flush(self) -> None: pass


async def stream(chunks: AsyncIterator[bytes], *, progress: Any = None) -> str:
    """Run one ASR session. Writes incremental partials to `progress`
    (any object with .write/.flush; default sys.stdout). Returns final text."""
    sink = progress if progress is not None else sys.stdout
    headers = {
        "X-Api-App-Key":     APPID,
        "X-Api-Access-Key":  TOKEN,
        "X-Api-Resource-Id": RESOURCE,
        "X-Api-Request-Id":  str(uuid.uuid4()),
        "X-Api-Connect-Id":  str(uuid.uuid4()),
    }

    try:
        async with websockets.connect(
            ENDPOINT, additional_headers=headers, max_size=10_000_000,
        ) as ws:
            await ws.send(frame(HDR_FULL_CLIENT, config_payload()))
            ack = parse(await ws.recv())
            if "_error" in ack:
                raise RuntimeError(f"config rejected: {ack}")

            done = asyncio.Event()
            last_text = ""

            async def sender() -> None:
                try:
                    prev: bytes | None = None
                    async for chunk in chunks:
                        if prev is not None:
                            await ws.send(frame(HDR_AUDIO, prev))
                        prev = chunk
                    await ws.send(frame(HDR_AUDIO_LAST, prev or b"\x00\x00"))
                except Exception:
                    done.set()
                    raise

            async def receiver() -> None:
                nonlocal last_text
                try:
                    async for msg in ws:
                        r = parse(msg)
                        if "_error" in r:
                            print(f"\nerr {r}", file=sys.stderr)
                            return
                        res = r.get("result")
                        if res:
                            text = (res.get("text", "") if isinstance(res, dict)
                                    else res[0].get("text", ""))
                            last_text = text
                            sink.write("\r\033[K" + text)
                            sink.flush()
                        if (r.get("_seq") or 0) < 0 or (r.get("_flags", 0) & 0b0010):
                            return
                finally:
                    done.set()

            send_task = asyncio.create_task(sender())
            recv_task = asyncio.create_task(receiver())
            try:
                await done.wait()
            finally:
                for t in (send_task, recv_task):
                    t.cancel()
                await asyncio.gather(send_task, recv_task, return_exceptions=True)
            sink.write("\n")
            sink.flush()
            return last_text
    except websockets.exceptions.InvalidStatus as e:
        lines = [f"handshake failed: HTTP {e.response.status_code}"]
        for k, v in e.response.headers.raw_items():
            if k.lower().startswith(("x-tt-", "x-api-", "x-request")):
                lines.append(f"  {k}: {v}")
        raise RuntimeError("\n".join(lines)) from e


# ---------- top-level modes ----------

async def daemon_main() -> None:
    """Mic record until SIGTERM/SIGINT; progress→stderr, final→stdout (one line)."""
    import signal
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    text = await stream(mic_chunks(stop=stop), progress=sys.stderr)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


async def ime_daemon() -> None:
    """Hold-to-talk daemon: record until a client connects to SOCK_PATH;
    on connect, end recording, wait for ASR final, write final text to client."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    text_ready: asyncio.Future[str] = loop.create_future()
    handlers: set[asyncio.Task] = set()

    async def handle_client(_reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        try:
            stop.set()
            try:
                text = await asyncio.wait_for(asyncio.shield(text_ready), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                text = ""
            try:
                writer.write(text.encode("utf-8") + b"\n")
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        finally:
            if task is not None:
                handlers.discard(task)

    SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCK_PATH.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(handle_client, path=str(SOCK_PATH))
    try:
        try:
            text = await stream(mic_chunks(stop=stop), progress=_NullSink())
        except Exception as e:
            print(f"talky daemon: {e!r}", file=sys.stderr)
            text = ""
        if not text_ready.done():
            text_ready.set_result(text)
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
    finally:
        server.close()
        await server.wait_closed()
        SOCK_PATH.unlink(missing_ok=True)


async def ime_stop() -> None:
    """Connect to daemon, fetch final text, type via xdotool. No-op if no daemon."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SOCK_PATH)), timeout=2.0,
        )
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError):
        return
    try:
        data = await asyncio.wait_for(reader.read(), timeout=12.0)
    except asyncio.TimeoutError:
        data = b""
    finally:
        writer.close()
    text = data.decode("utf-8", "replace").rstrip("\r\n").replace("\n", " ")
    if not text:
        return
    for selection in ["primary", "clipboard"]:
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", selection,
            stdin=asyncio.subprocess.PIPE,
        )
        await proc.communicate(text.encode())
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "key", "--clearmodifiers", "shift+Insert",
    )
    await proc.wait()


def _daemon_alive() -> bool:
    """Non-destructive liveness check via PID file. Connecting to SOCK_PATH
    would itself signal stop, so we cannot use the control socket as a probe."""
    try:
        pid = int(PID_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def ime_start() -> None:
    """Spawn ourselves as a detached IME daemon. No-op if one is already alive."""
    if _daemon_alive():
        return
    PID_PATH.unlink(missing_ok=True)
    SOCK_PATH.unlink(missing_ok=True)

    if os.fork() != 0:
        return
    os.setsid()
    log = os.open(str(LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(devnull)
    os.close(log)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(f"{os.getpid()}\n")
    try:
        asyncio.run(ime_daemon())
    finally:
        PID_PATH.unlink(missing_ok=True)
        os._exit(0)


# ---------- entry ----------

def _need_creds() -> None:
    global APPID, TOKEN, RESOURCE
    keys = ("VOLC_APPID", "VOLC_TOKEN", "VOLC_RESOURCE")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing env: {', '.join(missing)}")
    APPID, TOKEN, RESOURCE = (os.environ[k] for k in keys)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    mode = sys.argv[1]

    if mode == "stop":
        asyncio.run(ime_stop())
        return

    _need_creds()

    try:
        if mode == "start":
            ime_start()
        elif mode == "daemon":
            asyncio.run(daemon_main())
        elif mode in ("file", "mic"):
            if len(sys.argv) < 3:
                sys.exit(__doc__)
            arg = sys.argv[2]
            chunks = file_chunks(arg) if mode == "file" else mic_chunks(float(arg))
            asyncio.run(stream(chunks))
        else:
            sys.exit(f"unknown mode: {mode}")
    except (RuntimeError, ValueError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
