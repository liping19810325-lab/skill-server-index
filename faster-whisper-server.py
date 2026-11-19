#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
漫流 AI 剪辑台 · 本机 faster-whisper 转写服务（OpenAI 兼容，监听 127.0.0.1:9000）

只监听本机，不对外网开放。提供：
  GET  /v1/models                 模型列表（供 manflows-local-service.py 探测）
  GET  /docs                      简单说明
  GET  /                          健康检查
  POST /v1/audio/transcriptions   接收 multipart file，返回 OpenAI verbose_json

依赖：pip install faster-whisper （建议用独立虚拟环境 ~/.manflows/fw-venv）
模型：默认 small（首次运行自动从 HuggingFace 下载到 ~/.cache/huggingface，约 460MB）；
      可用 FW_MODEL 环境变量改成 tiny/base/medium/large-v3，FW_DEVICE=cpu（Apple Silicon 用 cpu+int8 即可）。
"""
import os, sys, json, io, wave, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("FW_PORT", "9000"))
MODEL = os.environ.get("FW_MODEL", "small")
DEVICE = os.environ.get("FW_DEVICE", "cpu")
COMPUTE = os.environ.get("FW_COMPUTE", "int8")

_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            print("[faster-whisper] 加载模型 %s device=%s compute=%s（首次会下载，请稍候）..." %
                  (MODEL, DEVICE, COMPUTE), flush=True)
            _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
            print("[faster-whisper] 模型就绪", flush=True)
        return _model


def _collect_segments(segments):
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if not text:
            continue
        words = []
        for w in (getattr(s, "words", None) or []):
            wt = (getattr(w, "word", None) or "").strip()
            if not wt:
                continue
            words.append({"word": wt, "start": round(float(w.start), 3), "end": round(float(w.end), 3)})
        out.append({"start": round(s.start, 3), "end": round(s.end, 3), "text": text, "words": words})
    return out


def transcribe(wav_path, language, word_timestamps=True):
    """字级时间戳：开启 word_timestamps 后每个 segment 带 words=[{word,start,end}]。
    AI博主字幕的动态字幕/花字动效严格绑定这些字级锚点；失败时前端会按字数插值降级。

    重要（回归修复）：某些 faster-whisper 版本/模型在 word_timestamps=True 时，
    word alignment 会把低置信 token 连同整段一起丢掉，结果返回 0 句且不抛异常。
    后端探测正常 + 0 句 + 无错误，会被上层误判成「素材没有人声」。
    因此这里做逐级降级重试，任何一级拿到句子立即返回：
      ① word_timestamps=True  + VAD（首选，动效能绑定字级锚点）
      ② word_timestamps=False + VAD（关掉 word alignment，拿到句子；动效走前端插值）
      ③ vad_filter=False            （关掉 VAD，救回音量小/被背景音盖住的口播）
    """
    model = get_model()
    lang_kwargs = {}
    if language and language != "auto":
        lang_kwargs["language"] = language

    def run(want_words, vad):
        kw = dict(beam_size=5, vad_filter=vad, word_timestamps=bool(want_words))
        if vad:
            kw["vad_parameters"] = dict(min_silence_duration_ms=500)
        kw.update(lang_kwargs)
        return model.transcribe(wav_path, **kw)

    diag = []
    # ① 首选：带字级时间戳
    segments, info = run(bool(word_timestamps), True)
    out = _collect_segments(segments)
    diag.append("① 字级时间戳%s：%d 句" % ("开" if word_timestamps else "关", len(out)))

    # ② 0 句时关掉 word alignment 重试（本次回归的直接修复点）
    if not out and word_timestamps:
        segments, info = run(False, True)
        out = _collect_segments(segments)
        word_timestamps = False
        diag.append("② 关字级时间戳重试：%d 句" % len(out))

    # ③ 仍 0 句时关掉 VAD，救回弱人声口播
    if not out:
        segments, info = run(False, False)
        out = _collect_segments(segments)
        diag.append("③ 关 VAD 重试：%d 句" % len(out))

    return out, getattr(info, "language", None), {
        "wordTimestamps": bool(word_timestamps), "diagnostics": diag}


def to_wav(data, filename):
    """用 faster-whisper 依赖的 av 解码任意容器（webm/mp4/mov/mp3/wav）到 16k 单声道 wav。"""
    import av
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="fw-")
    os.close(fd)
    with av.open(io.BytesIO(data)) as src:
        audio_stream = next((s for s in src.streams.audio), None)
        if audio_stream is None:
            raise RuntimeError("素材没有声音轨")
        with av.open(path, "w", format="wav") as dst:
            out_stream = dst.add_stream("pcm_s16le", rate=16000)
            out_stream.layout = "mono"
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            # 位置参数传 stream：av 14 / 19 都支持
            for frame in src.decode(audio_stream):
                for rf in resampler.resample(frame):
                    for pkt in out_stream.encode(rf):
                        dst.mux(pkt)
            for rf in resampler.resample(None):   # flush 尾部残留
                for pkt in out_stream.encode(rf):
                    dst.mux(pkt)
            for pkt in out_stream.encode():
                dst.mux(pkt)
    if os.path.getsize(path) < 44:
        raise RuntimeError("抽取的音轨为空")
    return path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[fw] " + (fmt % args) + "\n")

    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [
                {"id": "whisper-1", "object": "model", "owned_by": "faster-whisper-local"},
                {"id": MODEL, "object": "model", "owned_by": "faster-whisper-local"}]})
        elif p in ("/docs", "/", "/health"):
            self._send(200, {"ok": True, "service": "manflows faster-whisper",
                             "model": MODEL, "device": DEVICE, "compute": COMPUTE})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p not in ("/v1/audio/transcriptions", "/audio/transcriptions"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            # 从 multipart 提取 file 字段与 language
            language = "zh"
            import re
            m = re.search(rb'name="language"\r\n\r\n([^\r]*)', raw)
            if m:
                language = m.group(1).decode("utf-8", "replace").strip() or "zh"
            filename = "audio.wav"
            mf = re.search(rb'filename="([^"]+)"', raw)
            if mf:
                filename = mf.group(1).decode("utf-8", "replace")
            # 定位二进制文件内容：第一个 \r\n\r\n 之后到下一个 \r\n--boundary
            ctype = self.headers.get("Content-Type", "")
            bm = re.search(r'boundary=([^;]+)', ctype)
            boundary = bm.group(1).strip().strip('"').encode() if bm else b"----fw"
            marker = b"--" + boundary
            parts = raw.split(marker)
            filedata = b""
            for part in parts:
                if b'name="file"' in part and b"\r\n\r\n" in part:
                    chunk = part.split(b"\r\n\r\n", 1)[1]
                    if chunk.endswith(b"\r\n"):
                        chunk = chunk[:-2]
                    filedata = chunk
                    break
            if not filedata:
                raise RuntimeError("multipart 里没有找到 file 字段")
            wav_path = to_wav(filedata, filename)
            try:
                segments, lang, meta = transcribe(wav_path, language)
            finally:
                try: os.unlink(wav_path)
                except Exception: pass
            full_text = " ".join(s["text"] for s in segments)
            self._send(200, {"text": full_text, "language": lang or language,
                             "segments": segments,
                             "wordTimestamps": meta.get("wordTimestamps", False),
                             "diagnostics": meta.get("diagnostics", [])})
        except Exception as e:
            self._send(500, {"error": {"message": str(e)[:400]}})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("[faster-whisper] 本机转写服务已启动：http://127.0.0.1:%d/v1/audio/transcriptions" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
