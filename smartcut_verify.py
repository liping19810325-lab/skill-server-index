#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能剪口播 10 秒验证用例。

生成一段 10 秒「有停顿的口播」测试片（纯 FFmpeg 合成，也可选用 macOS say 生成中文人声），
然后验证本机服务的三个关键能力：
  1. /api/probe    能读到素材真实时长、独立视频/音频流；
  2. /api/silences 在没有任何 Whisper / 第三方接口的情况下，独立检出静音/气口区间；
  3. /api/render   把「删除区间」转成真实媒体重处理（重新解码、裁剪、拼接、重编码），
                    输出时长等于保留时长，而不是只改轨道预览。

用法（先双击启动本机服务）：
    python3 smartcut_verify.py
    MF_SERVICE=http://127.0.0.1:8788 python3 smartcut_verify.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

SERVICE = os.environ.get("MF_SERVICE", "http://127.0.0.1:8788").rstrip("/")
TIMEOUT = 180
PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail)[:400] if detail else ""))


def find_ffmpeg():
    local = os.path.expanduser("~/.manflows/bin/ffmpeg")
    if os.path.isfile(local):
        return local
    for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.path.isfile(p):
            return p
    return "ffmpeg"


def post(path, payload):
    req = urllib.request.Request(
        SERVICE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(SERVICE + path, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def make_test_video(ff, td):
    """10 秒：说话 2s → 停顿 1.2s → 说话 2s → 停顿 1s → 说话 2s → 停顿 1.8s。"""
    import math
    import wave
    audio = os.path.join(td, "speech.wav")
    # 纯标准库写 WAV，避免 ffmpeg aevalsrc 表达式里的逗号被当成滤镜分隔符
    rate, secs = 16000, 10.0
    segments = [(0.0, 2.0, 220.0), (3.2, 5.2, 260.0), (6.2, 8.2, 240.0)]
    with wave.open(audio, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        for i in range(int(rate * secs)):
            t = i / float(rate)
            freq = next((f for a, b, f in segments if a <= t < b), 0.0)
            val = 0 if not freq else int(8192 * math.sin(2 * math.pi * freq * t))
            w.writeframesraw(val.to_bytes(2, "little", signed=True))
    out = os.path.join(td, "talk_10s.mp4")
    subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
                    "-i", audio, "-shortest", "-t", "10",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", "48000", out], check=True)
    return out


def maybe_say_speech(td):
    """macOS 上可选：生成真中文人声（含语气词与停顿），用于人工试听。"""
    if sys.platform != "darwin":
        return None
    text = ("嗯，大家好，那个，今天我们来讲一下这个视频剪辑。"
            "[[slnc 1200]]啊，其实呢，这个功能很实用。"
            "[[slnc 900]]然后，我们来看一下具体的操作。"
            "[[slnc 1500]]那个，就到这里，谢谢大家。")
    aiff = os.path.join(td, "zh_filler.aiff")
    try:
        subprocess.run(["say", "-v", "Meijia", "-o", aiff, text], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return aiff if os.path.getsize(aiff) > 1000 else None
    except Exception:
        return None


def main():
    print("智能剪口播验证 · 服务 " + SERVICE)
    try:
        ping = get("/api/ping")
        chk("本机服务在线", bool(ping.get("ok")), ping.get("version"))
        chk("FFmpeg 可用", bool((ping.get("ffmpeg") or {}).get("found")))
    except Exception as e:
        chk("本机服务在线", False, e)
        print("请先双击「启动本机服务.command」(Mac) 或「启动本机服务.bat」(Windows)")
        print("FAIL: 服务不可用，后续用例跳过")
        sys.exit(1)

    ff = find_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="manflows-smartcut-") as td:
        video = make_test_video(ff, td)
        raw = b64(video)

        pr = post("/api/probe", {"filename": "talk_10s.mp4", "dataBase64": raw})
        chk("probe 读到 10 秒时长", bool(pr.get("ok")) and 9.5 <= float(pr.get("duration") or 0) <= 10.5,
            {"duration": pr.get("duration"), "hasVideo": pr.get("hasVideo"), "hasAudio": pr.get("hasAudio")})
        chk("probe 识别独立视频流与音频流", bool(pr.get("hasVideo")) and bool(pr.get("hasAudio")))

        si = post("/api/silences", {"filename": "talk_10s.mp4", "dataBase64": raw,
                                    "silenceDb": -35, "silenceMin": 0.5, "keepPad": 0.12})
        sil = si.get("silences") or []
        chk("无 Whisper 也独立检出静音", bool(si.get("ok")) and len(sil) >= 2,
            {"count": len(sil), "log": si.get("log")})
        chk("静音返回毫秒级时间戳", all("startMs" in z and "endMs" in z for z in sil),
            sil[:2])
        chk("切口留白生效", all(abs(float(z.get("keepPad") or 0) - 0.12) < 1e-6 for z in sil),
            sil[:1])
        chk("静音原始数据可供日志面板展示", bool((si.get("raw") or {}).get("events")),
            (si.get("raw") or {}).get("filter"))

        # 模拟「删掉第一段停顿 4.0~5.2 秒」：保留 [0,4] 与 [5.2,10] 两段源时间
        job = post("/api/render", {"filename": "talk_10s.mp4", "kind": "video",
                                   "width": 320, "height": 240, "fps": 15, "dataBase64": raw,
                                   "segments": [
                                       {"srcStart": 0.0, "srcLen": 4.0, "outLen": 4.0,
                                        "speed": 1.0, "reverse": False, "pitch": False},
                                       {"srcStart": 5.2, "srcLen": 4.8, "outLen": 4.8,
                                        "speed": 1.0, "reverse": False, "pitch": False}]})
        chk("删除区间提交真渲染任务", bool(job.get("ok")) and bool(job.get("jobId")), job)
        if job.get("ok"):
            import time
            result = None
            for _ in range(240):
                st = get("/api/render/status?jobId=" + job["jobId"])
                if st.get("state") == "done":
                    result = st
                    break
                if st.get("state") == "error":
                    chk("删除区间真渲染完成", False, st.get("reason"))
                    break
                time.sleep(0.25)
            if result:
                out = os.path.join(td, "cut_rendered.mp4")
                with open(out, "wb") as f:
                    f.write(base64.b64decode(result.get("dataBase64") or ""))
                rp = post("/api/probe", {"filename": "cut_rendered.mp4", "dataBase64": b64(out)})
                d = float(rp.get("duration") or 0)
                chk("删除区间真渲染完成且时长=保留时长", 8.4 <= d <= 9.4,
                    {"duration": d, "engine": result.get("engine")})
                chk("真渲染输出含画面与声音", bool(rp.get("hasVideo")) and bool(rp.get("hasAudio")))

        speech = maybe_say_speech(td)
        if speech:
            sr = post("/api/silences", {"filename": "zh_filler.wav", "dataBase64": b64(speech),
                                        "silenceDb": -35, "silenceMin": 0.5, "keepPad": 0.12})
            chk("中文口播素材（含语气词）检出停顿", bool(sr.get("ok")) and len(sr.get("silences") or []) >= 2,
                {"count": len(sr.get("silences") or [])})

    print("\n=== 汇总: %d/%d 通过 ===" % (len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()
