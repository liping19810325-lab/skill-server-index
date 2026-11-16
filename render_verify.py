#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
漫流 AI 剪辑台 · 倒放 / 变速真渲染验证用例（10 秒短视频）

用途
----
验证「导出成片」里的倒放、0.25x~5x 变速、倒放+变速叠加，确实是
FFmpeg 重新解码 / 重排帧 / 重编码后的文件效果，而不是 playbackRate 假效果。

运行
----
    python3 render_verify.py

脚本会：
  1. 用本机 FFmpeg 现场生成 10 秒测试片（画面亮度随时间 0→255 线性上升
     + 440Hz 正弦音，方便用「首末帧亮度方向」判断有没有真的反转帧顺序）；
  2. 若 127.0.0.1:8788 没在跑，自动拉起同目录的 manflows-local-service.py；
  3. 逐档调用 POST /api/render，轮询 GET /api/render/status 拿进度与结果；
  4. 对导出文件做时长 / 帧数 / 音轨 / 首末帧亮度校验；
  5. 打印结果表格，输出文件留在 /tmp/manflows-render-verify/ 供人工复看。

测试片与导出文件都不入库，仓库只保留这一个脚本。
"""

import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVICE = HERE / "manflows-local-service.py"
BASE = "http://127.0.0.1:8788"
OUT_DIR = Path(tempfile.gettempdir()) / "manflows-render-verify"
CHUNK = 2.0                       # 与服务端 RENDER_CHUNK_SEC 保持一致
SRC_SEC = 10.0
FPS = 30


# ---------------------------------------------------------------- FFmpeg 定位
def find_ffmpeg():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cands = [shutil.which("ffmpeg"),
             str(Path.home() / ".manflows" / "bin" / exe),
             "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
             "/usr/bin/ffmpeg",
             r"C:\ffmpeg\bin\ffmpeg.exe",
             r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


FF = find_ffmpeg()


def ff_run(args, timeout=300):
    p = subprocess.run([FF] + args, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    return p


def probe(path):
    """不依赖 ffprobe：解析 ffmpeg -i 的 stderr。"""
    p = ff_run(["-hide_banner", "-i", path], timeout=60)
    txt = (p.stderr or b"").decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", txt)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    has_v, has_a = "Video:" in txt, "Audio:" in txt
    n = ff_run(["-hide_banner", "-i", path, "-f", "null", "-"], timeout=180)
    tail = (n.stderr or b"").decode("utf-8", "replace")
    frames = re.findall(r"frame=\s*(\d+)", tail)
    return {"duration": dur, "video": has_v, "audio": has_a,
            "frames": int(frames[-1]) if frames else 0}


def frame_luma(path, first=True):
    """取第一帧（或反向后的第一帧＝最后一帧）的平均亮度，0~255。"""
    w, h = 160, 90
    vf = "format=gray" if first else "reverse,format=gray"
    p = ff_run(["-hide_banner", "-loglevel", "error", "-i", path,
                "-vf", vf, "-frames:v", "1", "-f", "rawvideo",
                "-pix_fmt", "gray", "-"], timeout=60)
    data = p.stdout or b""
    if len(data) < w * h:
        return -1
    return sum(data[:w * h]) / float(w * h)


# ---------------------------------------------------------------- 分段表（与剪辑台同算法）
def build_segments(duration, speed=1.0, curve=None, reverse=False,
                   offset=0.0, pitch=False, chunk=CHUNK):
    """先按输出时间轴切段（每段恒定速度），倒放时整表逆序 + 段内 reverse。"""
    if curve:
        profile = [(a * duration, (b - a) * duration, speed * mul)
                   for a, b, mul in curve]
    else:
        profile = [(0.0, duration, speed)]
    pieces, cursor = [], offset
    for _, out_len, sp in profile:
        n = max(1, int(math.ceil(out_len / chunk)))
        step = out_len / n
        for _ in range(n):
            src_len = step * sp
            pieces.append({"srcStart": round(cursor, 6), "srcLen": round(src_len, 6),
                           "outLen": round(step, 6), "speed": round(sp, 6),
                           "reverse": bool(reverse), "pitch": bool(pitch)})
            cursor += src_len
    if reverse:
        pieces.reverse()
    return pieces


# ---------------------------------------------------------------- 服务
def http(path, payload=None, timeout=30):
    url = BASE + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ping():
    try:
        return bool(http("/api/ping", timeout=3).get("ok"))
    except Exception:
        return False


def ensure_service():
    if ping():
        return None
    if not SERVICE.is_file():
        raise RuntimeError("找不到本机服务：%s" % SERVICE)
    p = subprocess.Popen([sys.executable, str(SERVICE)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if ping():
            return p
        time.sleep(0.25)
    raise RuntimeError("本机服务启动失败（127.0.0.1:8788）")


def render(src, segs, tag, kind="video"):
    with open(src, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    j = http("/api/render", {"filename": os.path.basename(src),
                             "dataBase64": b64, "kind": kind,
                             "width": 320, "height": 180, "fps": FPS,
                             "segments": segs}, timeout=60)
    if not j.get("ok"):
        raise RuntimeError("提交渲染失败：%s" % j.get("reason"))
    jid = j["jobId"]
    last = ""
    for _ in range(600):
        st = http("/api/render/status?jobId=" + jid, timeout=30)
        stage = "%s %d%%" % (st.get("stage", ""), round(st.get("progress", 0) * 100))
        if stage != last:
            print("    · %s" % stage)
            last = stage
        if st.get("state") == "done":
            out = OUT_DIR / (tag + ".mp4")
            out.write_bytes(base64.b64decode(st["dataBase64"]))
            return out, st
        if st.get("state") == "error":
            raise RuntimeError("渲染失败：%s" % st.get("reason"))
        time.sleep(0.3)
    raise RuntimeError("渲染超时")


# ---------------------------------------------------------------- 主流程
def main():
    global FF
    if not FF:
        print("❌ 没找到 FFmpeg。请先安装：")
        print("   macOS  : brew install ffmpeg  | https://evermeet.cx/ffmpeg/")
        print("   Windows: https://www.gyan.dev/ffmpeg/builds/ | winget install --id Gyan.FFmpeg")
        print("   Linux  : sudo apt install ffmpeg")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = OUT_DIR / "source_10s.mp4"
    print("== 生成 10 秒测试片 ==")
    p = ff_run(["-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=160x90:r=%d:d=%g" % (FPS, SRC_SEC),
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=%g" % SRC_SEC,
                "-vf", "geq=lum='255*T/%g':cb=128:cr=128" % SRC_SEC,
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", str(src)], timeout=180)
    if p.returncode != 0 or not src.is_file():
        print("❌ 测试片生成失败：", (p.stderr or b"").decode("utf-8", "replace")[-400:])
        return 1
    si = probe(src)
    print("   源片：%.2f 秒 / %d 帧 / 音轨 %s / 首帧亮度 %.0f → 末帧亮度 %.0f"
          % (si["duration"], si["frames"], "有" if si["audio"] else "无",
             frame_luma(src, True), frame_luma(src, False)))

    proc = ensure_service()
    print("== 本机服务已就绪 ==")
    cases = [
        ("对照_1x",       1.0,  False, "原速不处理",       10.0, "video"),
        ("变速_0.25x",    0.25, False, "慢放 4 倍",        40.0, "video"),
        ("变速_0.5x",     0.5,  False, "慢放 2 倍",        20.0, "video"),
        ("变速_2x",       2.0,  False, "快放 2 倍",         5.0, "video"),
        ("变速_5x",       5.0,  False, "快放 5 倍",         2.0, "video"),
        ("倒放_1x",       1.0,  True,  "帧序与采样全反转", 10.0, "video"),
        ("倒放_叠加2x",   2.0,  True,  "先变速再倒放",      5.0, "video"),
        ("音轨_倒放2x",   2.0,  True,  "独立音轨反向采样",  5.0, "audio"),
    ]
    rows, failed = [], []
    try:
        for tag, sp, rev, desc, want_dur, kind in cases:
            print("== %s：%s ==" % (tag, desc))
            # 剪辑台里 c.duration 是「变速后」的时长，源消耗 = 输出时长 × 倍率 = 10 秒
            segs = build_segments(SRC_SEC / sp, speed=sp, reverse=rev)
            out, st = render(str(src), segs, tag, kind)
            r = probe(str(out))
            ok_dur = abs(r["duration"] - want_dur) <= max(0.35, want_dur * 0.06)
            if kind == "audio":
                # 纯音频片段：检查时长与音轨存在、且确实没有视频轨
                l0 = l1 = 0
                ok_frames = True
                ok_audio = r["audio"] and not r["video"]
                ok_dir = True
                luma_txt = "无画面"
            else:
                l0, l1 = frame_luma(str(out), True), frame_luma(str(out), False)
                ok_frames = r["frames"] >= int(want_dur * FPS * 0.85)
                ok_audio = r["audio"] and r["video"]
                ok_dir = (l0 > l1 + 20) if rev else (l1 > l0 + 20)
                luma_txt = "%.0f→%.0f" % (l0, l1)
            ok = ok_dur and ok_frames and ok_audio and ok_dir
            if not ok:
                failed.append(tag)
            rows.append((tag, len(segs), "%.2fs" % r["duration"],
                         "%.2fs" % want_dur, r["frames"], luma_txt,
                         "有" if ok_audio else "缺",
                         "✅" if ok else "❌"))
            print("   → %d 段 / %s" % (len(segs), st.get("engine", "")))
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    print("\n== 验证结果（输出目录 %s）==" % OUT_DIR)
    head = ("用例", "段数", "实测时长", "期望时长", "帧数", "首→末亮度", "音轨", "结论")
    print("  %-14s %4s %10s %10s %7s %12s %5s %4s" % head)
    for r in rows:
        print("  %-14s %4d %10s %10s %7d %12s %5s %4s" % r)
    print("\n判定依据：时长误差 ≤6% 且 ≥0.35s 容差；帧数 ≥ 期望帧数 85%；"
          "音轨与画面同时存在；正放亮度递增、倒放亮度递减。")
    if failed:
        print("❌ 未通过：%s" % "、".join(failed))
        return 1
    print("✅ 全部通过：倒放、0.25x / 0.5x / 2x / 5x 变速、倒放+变速叠加，均为真实重编码文件。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("❌ 用例执行异常：%s" % e)
        sys.exit(1)
