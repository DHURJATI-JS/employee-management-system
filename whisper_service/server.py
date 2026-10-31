"""Meeting captions: faster-whisper (CTranslate2, int8 on the CPU) behind a tiny HTTP API. setup_services.ps1 runs it in Docker and
publishes it on 127.0.0.1 only: it has no login, so nothing outside this machine can reach it.
  GET  /health                     -> {"ready", "model", "detail"}
  POST /transcribe?language=xx     body: one audio clip (webm/opus, ogg, wav: anything ffmpeg reads), at most 4 MB
                                   -> {"language", "probability", "text", "seconds"}: the speech as said, in its own language
  POST /remux                      body: a MediaRecorder WebM (at most 4 GB) -> the same streams in a fresh WebM with an index (seekable)
The turbo model wasn't trained to translate (it answers in the spoken language even when asked to), so the app translates non-English
captions to English with its local LLM (meet.py)."""
import io, json, os, shutil, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
MODEL, THREADS, WORKERS = os.getenv("WHISPER_MODEL", "large-v3-turbo"), int(os.getenv("WHISPER_THREADS", "8")), int(os.getenv("WHISPER_WORKERS", "2"))
MAX_AUDIO, MAX_VIDEO = 4 << 20, 4 << 30
state = {"model": None, "detail": f"loading {MODEL} (the first start downloads it, about 1.6 GB)"}
class Heard:
    """The CTranslate2 model, remembering (per thread) the language it detected. faster-whisper's transcribe(language=None) runs the
    encoder twice (detect, then transcribe: ~8 s a clip on this CPU); transcribe(language=..., multilingual=True) detects it on the
    same encoder pass (~4 s), but doesn't say what it found: this does."""
    def __init__(self, model): self._model, self._seen = model, threading.local()
    def __getattr__(self, name): return getattr(self._model, name)
    def detect_language(self, *args, **kwargs):
        out = self._model.detect_language(*args, **kwargs)
        self._seen.lang = out[0][0]
        return out
    def heard(self) -> tuple[str, float]: token, prob = getattr(self._seen, "lang", ("<|en|>", 0.0)); return token[2:-2], prob
def load() -> None:
    try:
        from faster_whisper import WhisperModel
        state["model"] = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=THREADS, num_workers=WORKERS)
        state["model"].model = Heard(state["model"].model)
        state["detail"] = f"{MODEL} ready (int8 on the CPU, {THREADS} threads)"
    except Exception as e: state["detail"] = f"{MODEL} failed to load: {type(e).__name__}: {e}"[:300]
    print(state["detail"], flush=True)
def decode(data: bytes):  # any audio ffmpeg reads -> 16 kHz mono float32, what Whisper takes (faster-whisper's own decoder calls an av.open() option PyAV dropped)
    import av, numpy as np
    parts = []
    with av.open(io.BytesIO(data)) as box:
        resample = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in box.decode(audio=0): parts += [f.to_ndarray().reshape(-1) for f in resample.resample(frame)]
        parts += [f.to_ndarray().reshape(-1) for f in resample.resample(None)]
    return np.concatenate(parts).astype(np.float32) / 32768.0 if parts else np.zeros(0, np.float32)
def transcribe(audio: bytes, language: str | None) -> dict:
    started, m = time.monotonic(), state["model"]
    m.model._seen.lang = (f"<|{language}|>", 1.0) if language else ("<|en|>", 0.0)
    segments, _ = m.transcribe(decode(audio), language=language or "en", multilingual=not language, beam_size=1, vad_filter=True, condition_on_previous_text=False, without_timestamps=True)
    text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0).strip()  # drops what's probably not speech (whisper invents words in noise)
    lang, prob = m.model.heard()  # what it detected on the clip's first (only) 30 s
    return {"language": lang, "probability": round(prob, 3), "text": text, "seconds": round(time.monotonic() - started, 2)}
def remux(src_path: str, dst_path: str) -> None:  # MediaRecorder writes WebM as a stream (no duration, no cues): copying it into a new file adds both
    import av
    with av.open(src_path) as src, av.open(dst_path, "w", format="webm") as dst:
        keep = [s for s in src.streams if s.type in ("audio", "video")]
        out = {s.index: dst.add_stream_from_template(s) if hasattr(dst, "add_stream_from_template") else dst.add_stream(template=s) for s in keep}
        last = {}
        for p in src.demux(keep):
            if p.dts is None or p.dts < last.get(p.stream.index, -1): continue  # flush packets / out-of-order ones the muxer would refuse
            last[p.stream.index] = p.dts
            p.stream = out[p.stream.index]
            dst.mux(p)
PLAYABLE = {"mp4": ({"h264", "hevc", "av1"}, {"aac", "mp3", "opus"}), "webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"})}  # what browsers play, per container
def video(src_path: str, tmp: str, max_seconds: int) -> tuple[str, str, float]:
    """A shop video, rebuilt: its first video (+ audio) stream copied into a fresh MP4 (fast start) or WebM, nothing else (no metadata: a
    phone's location, the camera...). Refused when it isn't a video, is too long, or uses codecs browsers can't play. -> (path, ext, seconds)"""
    import av
    with av.open(src_path) as src:
        v, a = next(iter(src.streams.video), None), next(iter(src.streams.audio), None)
        if not v: raise ValueError("That file has no video in it.")
        seconds = float(src.duration / 1_000_000) if src.duration else float((v.duration or 0) * (v.time_base or 0))
        if seconds > max_seconds: raise ValueError(f"That video is {seconds:.0f} seconds long: they can be {max_seconds} at most.")
        vc, ac = v.codec_context.name, a.codec_context.name if a else None
        ext = next((e for e, (vs, aus) in PLAYABLE.items() if vc in vs and (ac is None or ac in aus)), None)
        if not ext: raise ValueError(f"Browsers can't play that video's format ({vc}{'/' + ac if ac else ''}): export it as MP4 (H.264 + AAC) or WebM.")
        dst = os.path.join(tmp, "out." + ext)
        with av.open(dst, "w", format=ext, container_options={"movflags": "+faststart"} if ext == "mp4" else {}) as out:
            keep = [s for s in (v, a) if s]
            streams = {s.index: out.add_stream_from_template(s) if hasattr(out, "add_stream_from_template") else out.add_stream(template=s) for s in keep}
            for p in src.demux(keep):
                if p.dts is None: continue
                p.stream = streams[p.stream.index]
                out.mux(p)
    return dst, ext, seconds
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args): pass
    def reply(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if urlsplit(self.path).path == "/health": return self.reply(200, {"ready": state["model"] is not None, "model": MODEL, "detail": state["detail"]})
        self.reply(404, {"detail": "not found"})
    def do_POST(self):
        url, n = urlsplit(self.path), int(self.headers.get("Content-Length") or 0)
        if url.path == "/transcribe":
            if not state["model"]: return self.reply(503, {"detail": state["detail"]})
            if not 0 < n <= MAX_AUDIO: return self.reply(413, {"detail": f"send 1 byte to {MAX_AUDIO >> 20} MB of audio"})
            lang = (parse_qs(url.query).get("language") or [""])[0]
            audio = self.rfile.read(n)
            try: return self.reply(200, transcribe(audio, lang if lang.isalpha() and len(lang) <= 3 else None))
            except Exception as e: return self.reply(422, {"detail": f"{type(e).__name__}: {e}"[:300]})
        if url.path == "/remux":
            if not 0 < n <= MAX_VIDEO: return self.reply(413, {"detail": "send 1 byte to 4 GB"})
            with tempfile.TemporaryDirectory() as tmp:
                src, dst, left = os.path.join(tmp, "in.webm"), os.path.join(tmp, "out.webm"), n
                with open(src, "wb") as f:
                    while left and (chunk := self.rfile.read(min(left, 1 << 20))): f.write(chunk); left -= len(chunk)
                try: remux(src, dst)
                except Exception as e: return self.reply(422, {"detail": f"{type(e).__name__}: {e}"[:300]})
                self.send_response(200); self.send_header("Content-Type", "video/webm"); self.send_header("Content-Length", str(os.path.getsize(dst))); self.end_headers()
                with open(dst, "rb") as f: shutil.copyfileobj(f, self.wfile, 1 << 20)
            return
        if url.path == "/video":
            if not 0 < n <= 64 << 20: return self.reply(413, {"detail": "Videos can be 64 MB at most."})
            limit = int((parse_qs(url.query).get("max_seconds") or ["180"])[0])
            with tempfile.TemporaryDirectory() as tmp:
                src, left = os.path.join(tmp, "in"), n
                with open(src, "wb") as f:
                    while left and (chunk := self.rfile.read(min(left, 1 << 20))): f.write(chunk); left -= len(chunk)
                try: dst, ext, seconds = video(src, tmp, max(1, min(limit, 600)))
                except ValueError as e: return self.reply(422, {"detail": str(e)[:300]})
                except Exception as e: return self.reply(422, {"detail": f"That file isn't a video we can use ({type(e).__name__})."})
                self.send_response(200); self.send_header("Content-Type", "video/" + ext); self.send_header("X-Ext", ext); self.send_header("X-Seconds", f"{seconds:.1f}")
                self.send_header("Content-Length", str(os.path.getsize(dst))); self.end_headers()
                with open(dst, "rb") as f: shutil.copyfileobj(f, self.wfile, 1 << 20)
            return
        self.reply(404, {"detail": "not found"})
if __name__ == "__main__":
    threading.Thread(target=load, daemon=True).start()
    print(f"whisper service on :9000, loading {MODEL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()  # inside the container; Docker publishes it on 127.0.0.1 only
