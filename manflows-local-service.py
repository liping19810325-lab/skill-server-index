#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
漫流 AI 剪辑台 · 本机小服务（跨平台 / Python 3 标准库 / 零 pip 依赖）

只监听 127.0.0.1:8788，供 https://www.manflows.com 的 editor.html 调用：
  GET  /api/ping            探测服务、平台、FFmpeg 状态
  GET  /api/guide           中文帮助
  GET  /api/ffmpeg-install  按需自动准备静态 FFmpeg
  GET/POST /api/resolve     把分享链接/直链解析成可播放地址
  GET  /api/media?u=...     流式代理媒体（Range / 防盗链 Referer）
  POST /api/export          FFmpeg 转码导出 / 抽音频 / 生成 GIF
  POST /api/transcribe      抽音轨 + 分段 + 本机/第三方转写（绕开浏览器 CORS）

所有响应都带 CORS 与 Chrome Private Network Access 头；错误一律返回 JSON，
不向外抛裸异常。被代理目标做基本 SSRF 防护。
"""

import base64
import ipaddress
import gzip
import json
import os
import platform
import re
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = 8788
VERSION = "1.0.0"

TIMEOUT_FETCH = 10        # 解析网页 / API 请求
TIMEOUT_DOWNLOAD = 15     # 下载静态 FFmpeg（socket 无数据超时）
TIMEOUT_MEDIA = 30        # 媒体流式代理
TIMEOUT_TRANSCODE = 600   # 转码 10 分钟
TIMEOUT_ASR = 180         # 单段语音转写
TIMEOUT_ASR_PROBE = 1.2   # 探测本机 faster-whisper
ASR_LOCAL_BASE = "http://127.0.0.1:9000"
ASR_CHUNK_SEC = 60        # 每段 60 秒（16k/mono WAV ≈ 1.9MB，远低于模型 20MB 上限）
CHUNK = 64 * 1024
MAX_BODY = 300 * 1024 * 1024  # 300MB 请求体上限

UA_DESKTOP = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

FFMPEG_GUIDE = {
    "mac": "https://brew.sh/ 与 https://evermeet.cx/ffmpeg/",
    "windows": "https://www.gyan.dev/ffmpeg/builds/",
    "linux": "sudo apt install ffmpeg",
}

ASR_INSTALL_GUIDE = (
    "本机没检测到 faster-whisper，也没有可用的第三方转写接口。"
    "最省事的做法是在本机终端执行一次：pip install faster-whisper-server "
    "（或 pip install faster-whisper && pip install uvicorn），"
    "再用默认 9000 端口启动 OpenAI 兼容的 /v1/audio/transcriptions；"
    "也可以改用提供 whisper-1 / gpt-4o-transcribe 通道的第三方 API，"
    "在创作台/画布「算力总控 → 第三方API」里填好地址与密钥即可（由本服务转发，不受浏览器跨域限制）。"
)

COMMON_FFMPEG = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]

MEDIA_EXT_KIND = {
    "mp4": "video", "m4v": "video", "webm": "video", "mov": "video",
    "mkv": "video", "avi": "video", "m3u8": "video", "mpd": "video",
    "mp3": "audio", "m4a": "audio", "wav": "audio", "aac": "audio",
    "flac": "audio", "ogg": "audio", "opus": "audio",
    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image",
    "gif": "image", "bmp": "image",
}

CT_BY_EXT = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "webm": "video/webm", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "m3u8": "application/vnd.apple.mpegurl", "mpd": "application/dash+xml",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
    "aac": "audio/aac", "flac": "audio/flac", "ogg": "audio/ogg",
    "opus": "audio/ogg", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp", "gif": "image/gif",
    "bmp": "image/bmp",
}

EXT_RE = re.compile(
    r"\.(mp4|m4v|webm|mov|mkv|avi|m3u8|mpd|mp3|m4a|wav|aac|flac|ogg|opus|"
    r"jpg|jpeg|png|webp|gif|bmp)(?=[?#&/]|$)", re.I)

HINT_WEBPAGE = (
    "请粘贴以 .mp4/.webm/.mov/.mp3/.m4a/.wav/.jpg/.png 等结尾的媒体直链；"
    "获取直链的方法：在浏览器中打开视频 → 按 F12 打开开发者工具 → "
    "网络(Network)面板筛选 media → 复制该请求的 URL；"
    "抖音/B站的分享链接也可直接粘贴进来由本服务尝试解析。"
)

# ----------------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------------
def _fail(reason, hint=""):
    return {"ok": False, "reason": reason, "hint": hint}


def _short(e):
    s = str(e).strip().replace("\n", " ")
    return s[:200] if s else e.__class__.__name__


def _plat():
    s = platform.system().lower()
    if s == "darwin":
        return "mac"
    if s == "windows":
        return "windows"
    return "linux"


def _arch():
    m = (platform.machine() or "").lower()
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return platform.machine() or "unknown"


def _python_ver():
    return platform.python_version()


def _scrub(text, ff="", td=""):
    if td:
        text = text.replace(td, "<tmp>")
    if ff:
        text = text.replace(ff, "ffmpeg")
    return text


def _scrub_cmd(cmd, ff, td):
    parts = []
    for c in cmd:
        c = _scrub(str(c), ff, td)
        parts.append('"%s"' % c if (" " in c and not c.startswith('"')) else c)
    return " ".join(parts)


# ----------------------------------------------------------------------------
# SSRF 基本防护
# ----------------------------------------------------------------------------
def is_allowed_target(url):
    """只允许公网 http/https；拒绝本机/回环/内网/保留地址与非 http(s) 协议。"""
    try:
        p = urlparse(url)
    except Exception:
        return False, "URL 格式无法解析"
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "只允许 http/https 协议（拒绝 file://、gopher://、ftp:// 等）"
    if p.username or p.password:
        return False, "URL 中不允许携带用户名/密码"
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False, "缺少主机名"
    if host == "localhost" or host.endswith(".localhost"):
        return False, "禁止访问本机/回环地址（localhost）"
    if host in ("metadata.google.internal", "metadata", "169.254.169.254"):
        return False, "禁止访问云元数据/内网地址"
    # 纯数字形式的 IP（如 2130706433 == 127.0.0.1）也拒绝
    if re.fullmatch(r"\d+", host):
        try:
            if 0 <= int(host) <= 0xFFFFFFFF:
                return False, "禁止使用数字形式的内网/回环地址"
        except ValueError:
            pass
    # IP 字面量：回环 / 私有 / 链路本地 / 保留 / 组播 / 未指定，一律拒绝
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_loopback or ip.is_private or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, "禁止访问内网/保留/回环地址：%s" % host
    except ValueError:
        pass  # 普通域名（抖音、B站、CDN 等公网域名）放行
    return True, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都做同样检查，防止 302 把请求带进内网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, why = is_allowed_target(newurl)
        if not ok:
            raise urllib.error.URLError("重定向被拒绝：%s" % why)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SSL_UNVERIFIED = False


def _unverified_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _build_opener(unverified=False):
    handlers = [_SafeRedirectHandler()]
    if unverified:
        handlers.append(urllib.request.HTTPSHandler(context=_unverified_ctx()))
    return urllib.request.build_opener(*handlers)


def _is_cert_error(e):
    s = str(e).lower()
    if any(k in s for k in ("certificate_verify_failed", "certificate verify failed",
                            "unable to get local issuer", "self signed certificate",
                            "self-signed certificate")):
        return True
    return type(getattr(e, "reason", None)).__name__ == "SSLCertVerificationError"


def _open_url(url, headers=None, timeout=TIMEOUT_FETCH):
    """正常校验 TLS；仅当本机环境证书链不全 / 代理 MITM 导致校验失败时，
    才降级为不校验重试，以保证 macOS 自带或企业代理环境下也能正常取流。"""
    global _SSL_UNVERIFIED
    ok, why = is_allowed_target(url)
    if not ok:
        raise ValueError(why)
    h = {"User-Agent": UA_DESKTOP, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        return _build_opener(_SSL_UNVERIFIED).open(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not _SSL_UNVERIFIED and _is_cert_error(e):
            _SSL_UNVERIFIED = True
            print("[提示] 本机 HTTPS 证书校验失败，已自动降级为不校验以保证取流可用；"
                  "在不可信网络下请注意。", file=sys.stderr)
            return _build_opener(True).open(req, timeout=timeout)
        raise


# ----------------------------------------------------------------------------
# FFmpeg 检测 / 自动安装
# ----------------------------------------------------------------------------
_FFMPEG_CACHE = None


def _parse_encoders(text):
    names = []
    for line in text.splitlines():
        m = re.match(r"^\s*[VAS][.A-Z]{5}\s+([A-Za-z0-9_][\w.\-]*)\s", line)
        if m:
            names.append(m.group(1))
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _ffmpeg_info(path):
    version, encoders = "", []
    try:
        p = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15)
        out = (p.stdout + p.stderr).decode("utf-8", "replace")
        m = re.search(r"version\s+([0-9][^\s]*)", out)
        version = m.group(1) if m else (out.splitlines()[0][:80] if out else "")
    except Exception:
        pass
    try:
        p = subprocess.run([path, "-hide_banner", "-encoders"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=15)
        encoders = _parse_encoders((p.stdout + p.stderr).decode("utf-8", "replace"))
    except Exception:
        pass
    return version, encoders


def detect_ffmpeg():
    """shutil.which → ~/.manflows/bin → 常见路径 → found:false。找到即用缓存。"""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE and _FFMPEG_CACHE.get("found"):
        return _FFMPEG_CACHE
    exe = "ffmpeg.exe" if _plat() == "windows" else "ffmpeg"
    candidates = []
    w = shutil.which("ffmpeg")
    if w:
        candidates.append(w)
    candidates.append(str(Path.home() / ".manflows" / "bin" / exe))
    candidates.extend(COMMON_FFMPEG)
    path = ""
    for c in candidates:
        try:
            if c and os.path.isfile(c) and (_plat() == "windows" or os.access(c, os.X_OK)):
                path = c
                break
        except Exception:
            pass
    if not path:
        return {"found": False, "path": "", "version": "", "encoders": []}
    version, encoders = _ffmpeg_info(path)
    res = {"found": True, "path": path, "version": version, "encoders": encoders}
    _FFMPEG_CACHE = res
    return res


def _download_binary(url, path, timeout=TIMEOUT_DOWNLOAD):
    """下载静态 FFmpeg：依次尝试「直连 → 环境变量代理 → 常见本地 VPN 端口」。
    某些本机环境的内置代理对 GitHub release 返回 502，而用户的本地 VPN
    端口可用；多通道重试可以让自动安装在两种网络环境下都尽量成功。"""
    ok, why = is_allowed_target(url)
    if not ok:
        raise ValueError(why)
    channels = [None]
    # 常见本地 VPN 端口优先于环境变量代理：不少开发环境注入的内置代理
    # 对 GitHub release 返回 502，而用户本机的 7890/7891 是可用的。
    for port in (7890, 7891, 1087):
        v = "http://127.0.0.1:%d" % port
        if v not in channels:
            channels.append(v)
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var)
        if v and v not in channels:
            channels.append(v)
    errs = []
    for px in channels:
        try:
            handlers = [_SafeRedirectHandler(),
                        urllib.request.ProxyHandler({} if px is None else {"http": px, "https": px})]
            if _SSL_UNVERIFIED:
                handlers.append(urllib.request.HTTPSHandler(context=_unverified_ctx()))
            op = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": UA_DESKTOP, "Accept": "*/*"})
            with op.open(req, timeout=timeout) as r, open(path, "wb") as f:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(path) < 100 * 1024:
                raise RuntimeError("下载内容异常小，可能不是可执行文件")
            return px or "直连"
        except Exception as e:
            errs.append("%s:%s" % (px or "直连", _short(e)))
            try: os.remove(path)
            except OSError: pass
    raise RuntimeError("所有下载通道均失败 → " + " | ".join(errs[-3:]))


def _download_to(url, path, timeout=TIMEOUT_DOWNLOAD):
    """兼容旧调用，内部走多通道下载。"""
    return _download_binary(url, path, timeout)


def api_ffmpeg_install():
    """仅在本接口被调用时才尝试联网下载，不在启动时强制安装。
       Mac 用 ffmpeg-static 的 darwin 原生二进制（.gz ~19MB），Windows 用
       gyan.dev 的 essentials zip；Mac 另有 evermeet 新 release API 作为备用。"""
    global _FFMPEG_CACHE
    info = detect_ffmpeg()
    if info["found"]:
        return {"ok": True, "path": info["path"], "version": info["version"],
                "hint": "本机已检测到 FFmpeg，无需安装。"}
    p = _plat()
    if p == "linux":
        return {"ok": False, "reason": "Linux 平台不提供自动下载，请用系统包管理器安装",
                "guide": FFMPEG_GUIDE,
                "hint": "请在终端执行 sudo apt install ffmpeg（或对应发行版命令）后重启本服务"}
    try:
        bin_dir = Path.home() / ".manflows" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        exe = "ffmpeg.exe" if p == "windows" else "ffmpeg"
        dest = bin_dir / exe
        urls = []
        if p == "windows":
            urls = ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"]
        else:
            tag = "b6.1.1"
            arch = "arm64" if _arch() == "arm64" else "x64"
            urls.append("https://github.com/eugeneware/ffmpeg-static/releases/download/%s/ffmpeg-darwin-%s.gz" % (tag, arch))
            try:
                with _open_url("https://evermeet.cx/ffmpeg/info/ffmpeg/release", timeout=8) as r:
                    meta = json.loads(r.read().decode("utf-8", "replace"))
                alt = ((meta.get("download") or {}).get("zip") or {}).get("url")
                if alt:
                    urls.append(alt)
            except Exception:
                pass
        last, done = "", False
        for url in urls:
            try:
                part = dest.with_name(dest.name + ".part")
                via = _download_binary(url, str(part), timeout=TIMEOUT_DOWNLOAD)
                low = url.lower()
                if low.endswith(".gz"):
                    with gzip.open(str(part), "rb") as gsrc, open(str(dest), "wb") as out:
                        shutil.copyfileobj(gsrc, out, CHUNK)
                    try: os.remove(str(part))
                    except OSError: pass
                elif low.endswith(".zip"):
                    zp = dest.with_suffix(".zip")
                    os.replace(str(part), str(zp))
                    with zipfile.ZipFile(str(zp)) as zf:
                        member = None
                        for n in zf.namelist():
                            if n.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("ffmpeg", "ffmpeg.exe"):
                                member = n
                                break
                        if not member:
                            raise RuntimeError("zip 中未找到 ffmpeg 可执行文件")
                        with zf.open(member) as src, open(str(dest), "wb") as out:
                            shutil.copyfileobj(src, out, CHUNK)
                    try: os.remove(str(zp))
                    except OSError: pass
                else:
                    os.replace(str(part), str(dest))
                if p != "windows":
                    os.chmod(str(dest), 0o755)
                done = True
                print("[本机服务] FFmpeg 已通过 %s 下载：%s" % (via, url), file=sys.stderr)
                break
            except Exception as e:
                last = _short(e)
                for f in (dest.with_name(dest.name + ".part"), dest.with_suffix(".zip")):
                    try: os.remove(str(f))
                    except OSError: pass
        if not done:
            raise RuntimeError("多源下载均失败（%s）" % last[:180])
        _FFMPEG_CACHE = None
        info = detect_ffmpeg()
        if not info["found"] or not info["path"]:
            raise RuntimeError("下载完成但 FFmpeg 仍不可用（可能被安全软件拦截、架构不匹配或文件损坏）")
        return {"ok": True, "path": info["path"], "version": info["version"],
                "hint": "FFmpeg 已安装到 %s，现在可以导出成片了。" % info["path"]}
    except Exception as e:
        return {"ok": False, "reason": "自动安装失败：%s" % _short(e),
                "guide": FFMPEG_GUIDE, "hint": "请手动安装 ffmpeg 后重启本服务"}


# ----------------------------------------------------------------------------
# /api/ping  /api/guide
# ----------------------------------------------------------------------------
def api_ping():
    info = detect_ffmpeg()
    if info["found"]:
        hint = "已检测到 FFmpeg（%s），可直接导出成片。" % (info["version"] or info["path"])
    else:
        hint = "未检测到 FFmpeg：点『导出成片』时会自动尝试安装，也可手动安装后重启本服务。"
    return {"ok": True, "port": PORT, "platform": _plat(), "arch": _arch(),
            "python": _python_ver(), "version": VERSION,
            "ffmpeg": info, "guide": FFMPEG_GUIDE, "hint": hint,
            "asrLocal": _asr_probe_local(), "asrGuide": ASR_INSTALL_GUIDE}


def api_guide():
    return {
        "ok": True,
        "service": "漫流 AI 剪辑台 · 本机服务",
        "version": VERSION,
        "port": PORT,
        "base": "http://127.0.0.1:%d" % PORT,
        "start": {
            "mac": "双击『启动本机服务.command』；若提示无法打开，右键选『打开』，或先执行 chmod +x 启动本机服务.command。",
            "windows": "双击『启动本机服务.bat』；若被安全软件拦截，选『仍要运行』。",
            "linux": "在终端执行：python3 manflows-local-service.py",
        },
        "keep": "启动后会出现一个终端窗口，请保持它开着；关闭窗口或按 Ctrl+C 即停止服务。浏览器里点『导出成片』会自动探测本服务。",
        "ffmpeg": {
            "mac": ["https://brew.sh/（装 Homebrew 后 brew install ffmpeg）",
                    "https://evermeet.cx/ffmpeg/（静态版）"],
            "windows": "https://www.gyan.dev/ffmpeg/builds/",
            "linux": "sudo apt install ffmpeg（或 yum/dnf/pacman 对应命令）",
            "auto": "也可直接点『导出成片』，服务会按需尝试下载静态 FFmpeg 到 ~/.manflows/bin/；失败请按上方链接手动安装后重启本服务。",
        },
        "resolve": {
            "direct": "最稳妥：粘贴以 .mp4/.webm/.mov/.mp3/.m4a/.wav/.jpg/.png 等结尾的媒体直链。",
            "douyin": "粘贴 v.douyin.com 短链、www.iesdouyin.com/share/video/... 或 www.douyin.com/video/... 分享链接，服务尝试从页面解析视频直链；抖音改版频繁，可能失效。",
            "bilibili": "粘贴含 BV 号的视频页链接或 b23.tv 短链，服务通过 B站公开接口取 cid 与播放直链，并带 Referer 通过防盗链。",
            "howto": "如何获取直链：浏览器打开视频 → F12 开发者工具 → 网络(Network) → 筛选 media → 复制该请求 URL 粘贴到输入框。",
        },
        "transcribe": {
            "what": "AI字幕 / 智能剪口播 的语音识别：网页把素材交给本服务，本服务用 FFmpeg 抽成 16kHz 单声道 WAV、按 60 秒切片，再逐段转发给转写接口并合并时间码。",
            "why": "浏览器直连第三方 API 会被 CORS 拦截（Failed to fetch），且整段 mp4 太大无法塞给模型；由本机服务转发与切片后这两个问题都不存在。",
            "engines": ["① 本机 faster-whisper（默认 127.0.0.1:9000，纯本地免费）",
                        "② 创作台/画布里填的第三方 OpenAI 兼容 /v1/audio/transcriptions（需 whisper-1 / gpt-4o-transcribe 通道）",
                        "③ 两者都没有时给出安装指引，绝不生成假字幕"],
            "install": ASR_INSTALL_GUIDE,
            "note": "Claude / 普通 GPT 对话模型不是语音转写模型，走的是 /v1/chat/completions；它们能跑 AI剪辑，但不能直接用来做 AI字幕。",
        },
        "privacy": "本服务只监听 127.0.0.1（本机回环地址），不对局域网/公网开放，不会上传任何媒体内容或个人信息；解析/播放时代理会访问对应的公网视频服务器，导出转码全程由本机 FFmpeg 在本机完成，AI字幕的音频也会在你选择第三方转写接口时才发往该接口，临时文件用完自动清理。",
        "errors": {
            "端口被占用": "8788 已被其它程序（很可能是另一个本服务实例）占用，先关闭旧窗口再启动。",
            "探测不到服务": "确认终端窗口还开着、浏览器是 https://www.manflows.com 的剪辑台页面、且未被安全软件拦截本机回环请求。",
            "提示安装 FFmpeg": "按 ffmpeg 里的链接安装后重启本服务，或让服务自动安装。",
            "上游 403/防盗链": "直链已过期或需要 Referer/Cookie，重新解析获取新直链。",
            "解析失败": "抖音/B站接口改版或风控，改用 .mp4 直链最稳。",
            "转码失败": "降低分辨率/码率，或换 H.264 + MP4；确认录制的中间文件可正常播放。",
        },
        "hint": "保持此窗口开着；浏览器里点『导出成片』会自动探测本服务。",
    }


# ----------------------------------------------------------------------------
# /api/resolve
# ----------------------------------------------------------------------------
def _proxy_url(u):
    return "http://127.0.0.1:%d/api/media?u=%s" % (PORT, quote(u, safe=""))


def _filename_title(url):
    p = urlparse(url)
    name = unquote((p.path or "").rstrip("/").split("/")[-1])
    return name[:80] if name else (p.hostname or "媒体")


def _looks_like_direct(url):
    """返回 (是否直链, kind)。路径扩展名或明显媒体查询参数命中即视为直链。"""
    p = urlparse(url)
    path = unquote(p.path or "")
    query = unquote(p.query or "")
    m = EXT_RE.search(path) or EXT_RE.search(query)
    if m:
        return True, MEDIA_EXT_KIND.get(m.group(1).lower(), "video")
    if re.search(r"(mime|format|type|suffix|fname|filename|file)=[^&]*"
                 r"(video|audio|mp4|mp3|m3u8|webm|mov)", query, re.I):
        return True, "video"
    return False, "text"


def _direct_result(url, kind):
    return {"ok": True, "kind": kind, "title": _filename_title(url),
            "via": "direct", "directUrl": url, "playUrl": _proxy_url(url),
            "note": "识别为媒体直链；播放仍经由本机 /api/media 代理，以统一处理跨域与防盗链。"}


def _find_first_str(node, keys):
    keys = set(k.lower() for k in keys)
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in keys and isinstance(v, str) and v.strip():
                return v.strip()
        for v in node.values():
            r = _find_first_str(v, keys)
            if r:
                return r
    elif isinstance(node, list):
        for it in node:
            r = _find_first_str(it, keys)
            if r:
                return r
    return ""


_MEDIA_KEYS = {"play_addr", "download_addr", "playapi", "play_addr_h264",
               "play_addr_265", "url_list", "backup_url", "play_url",
               "video_url", "main_url", "download_url"}


def _walk_urls(node, found, prefer=False):
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_urls(v, found, prefer or str(k).lower() in _MEDIA_KEYS)
    elif isinstance(node, list):
        for it in node:
            _walk_urls(it, found, prefer)
    elif isinstance(node, str):
        s = node.strip()
        if s.startswith("http://") or s.startswith("https://"):
            found.append((s, prefer))


def _pick_video_url(found):
    ordered = [u for u, p in found if p] + [u for u, p in found if not p]
    hint = ("aweme/v1/play", "/play/", "playwm", "video", ".mp4", "m3u8",
            "snssdk", "douyinvod", "bytecdn", "ixigua", "videocdn")
    videoish = [u for u in ordered if any(h in u for h in hint)]
    pool = videoish or ordered
    clean = [u for u in pool if "playwm" not in u]
    if clean:
        return clean[0]
    wm = [u for u in pool if "playwm" in u]
    if wm:
        return wm[0].replace("playwm", "play")
    return pool[0] if pool else None


def _extract_douyin_json(html):
    objs = []
    idx = html.find("_ROUTER_DATA")
    if idx >= 0:
        eq = html.find("=", idx)
        brace = html.find("{", eq)
        if brace >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(html[brace:])
                objs.append(obj)
            except Exception:
                pass
    m = re.search(r'id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', html, re.I | re.S)
    if m:
        raw = unquote(m.group(1).strip())
        try:
            objs.append(json.JSONDecoder().raw_decode(raw.lstrip())[0])
        except Exception:
            try:
                objs.append(json.loads(raw))
            except Exception:
                pass
    return objs


def _resolve_douyin(url):
    html, last_err = "", ""
    # 桌面 UA 优先；失败或页面无有效数据时换手机 UA 再试一次（缩短超时，
    # 避免前端 12s 解析超时前一直等待）。
    attempts = ((UA_DESKTOP, TIMEOUT_FETCH), (UA_MOBILE, 5))
    for ua, to in attempts:
        try:
            with _open_url(url, headers={"User-Agent": ua,
                                         "Accept-Language": "zh-CN,zh;q=0.9"},
                           timeout=to) as r:
                html = r.read(3 * 1024 * 1024).decode("utf-8", "replace")
            if html and any(k in html for k in
                            ("_ROUTER_DATA", "RENDER_DATA", "play_addr", "playwm",
                             "aweme/v1/play")):
                break
        except urllib.error.HTTPError as e:
            last_err = "抖音页面返回 HTTP %s（可能触发反爬或需要登录）" % e.code
            try:
                e.close()
            except Exception:
                pass
        except Exception as e:
            last_err = _short(e)
    objs = _extract_douyin_json(html)
    found = []
    for obj in objs:
        _walk_urls(obj, found)
    candidate = _pick_video_url(found)
    title = "抖音视频"
    for obj in objs:
        t = _find_first_str(obj, ("desc", "title", "share_title", "nickname"))
        if t:
            title = t[:80]
            break
    if title == "抖音视频":
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = (re.sub(r"\s+", " ", m.group(1)).strip()[:80] or title)
    if not candidate:
        return _fail("未能从抖音页面解析出视频直链" + ("（%s）" % last_err if last_err else ""),
                     "抖音页面结构或接口经常改版，本服务的解析规则可能失效。请在浏览器打开该视频，"
                     "按 F12 → 网络(Network) → 筛选 media，复制以 .mp4 结尾的直链粘贴；或改用抖音网页版分享链接。")
    direct = candidate.replace("playwm", "play")
    note = "已解析出抖音视频直链"
    if direct != candidate:
        note += "（已尝试去水印 playwm→play）"
    note += "；播放经由本机代理并带抖音 Referer。"
    return {"ok": True, "kind": "video", "title": title, "via": "douyin",
            "directUrl": direct, "playUrl": _proxy_url(direct), "note": note}


def _resolve_bilibili(url):
    bv = ""
    m = re.search(r"(BV[0-9A-Za-z]{8,12})", url)
    if m:
        bv = m.group(1)
    host = (urlparse(url).hostname or "").lower()
    if not bv or "b23.tv" in host:
        try:
            with _open_url(url, headers={"User-Agent": UA_DESKTOP,
                                         "Referer": "https://www.bilibili.com/"},
                           timeout=TIMEOUT_FETCH) as r:
                final = r.geturl()
                m2 = re.search(r"(BV[0-9A-Za-z]{8,12})", final)
                if m2:
                    bv = m2.group(1)
                elif not bv:
                    head = r.read(512 * 1024).decode("utf-8", "replace")
                    m2 = re.search(r"(BV[0-9A-Za-z]{8,12})", head)
                    if m2:
                        bv = m2.group(1)
        except Exception as e:
            return _fail("无法跟随 B站短链获取 BV 号：%s" % _short(e),
                         "请直接粘贴完整视频页链接（含 BV 号），例如 https://www.bilibili.com/video/BVxxxxxxxxxx")
    if not bv:
        return _fail("未能从链接中提取到 B站 BV 号",
                     "请使用形如 https://www.bilibili.com/video/BV1xx411c7mD 的视频页链接或 b23.tv 短链。")
    hdr = {"User-Agent": UA_DESKTOP, "Referer": "https://www.bilibili.com/"}
    try:
        with _open_url("https://api.bilibili.com/x/web-interface/view?bvid=" + quote(bv, safe=""),
                       headers=hdr, timeout=TIMEOUT_FETCH) as r:
            view = json.loads(r.read(1024 * 1024).decode("utf-8", "replace"))
    except Exception as e:
        return _fail("请求 B站视频信息失败：%s" % _short(e),
                     "可能是网络不通或 B站接口临时不可用，请稍后重试。")
    if view.get("code") != 0 or not isinstance(view.get("data"), dict):
        return _fail("B站接口返回异常：code=%s %s" % (view.get("code"), view.get("message", "")),
                     "该视频可能不存在、需要登录或受地区限制；也可改用 .mp4 直链。")
    cid = view["data"].get("cid")
    title = view["data"].get("title") or "B站视频"
    if not cid:
        return _fail("B站视频信息里缺少 cid",
                     "该视频可能是合集/分 P 或接口改版，请换用 .mp4 直链。")
    api = ("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=64&fnval=0"
           % (quote(bv, safe=""), quote(str(cid), safe="")))
    try:
        with _open_url(api, headers=hdr, timeout=TIMEOUT_FETCH) as r:
            pu = json.loads(r.read(1024 * 1024).decode("utf-8", "replace"))
    except Exception as e:
        return _fail("请求 B站播放地址失败：%s" % _short(e),
                     "可能是接口风控或网络问题，请稍后重试。")
    durl = (pu.get("data") or {}).get("durl") or []
    play = ""
    if isinstance(durl, list) and durl and isinstance(durl[0], dict):
        play = durl[0].get("url") or ""
        if not play:
            backup = durl[0].get("backup_url") or []
            if backup and isinstance(backup[0], str):
                play = backup[0]
    if not play:
        return _fail("B站未返回可直连的播放地址（可能仅提供 DASH 流或需要登录/大会员）",
                     "请降低清晰度重试，或在浏览器开发者工具里复制 .mp4 直链；也可粘贴已获取的视频直链。")
    ok, why = is_allowed_target(play)
    if not ok:
        return _fail("B站返回的播放地址不被允许：%s" % why, "请改用其它来源的媒体直链。")
    return {"ok": True, "kind": "video", "title": str(title)[:80], "via": "bilibili",
            "directUrl": play, "playUrl": _proxy_url(play),
            "note": "已解析 B站视频直链；本机代理播放会带 Referer: https://www.bilibili.com/ 以通过防盗链。"}


def api_resolve(url):
    url = (url or "").strip()
    if not url:
        return _fail("缺少 url 参数", "请传入要解析的链接，例如 {\"url\":\"https://.../a.mp4\"}")
    ok, why = is_allowed_target(url)
    if not ok:
        return _fail("链接不被允许：%s" % why,
                     "仅支持公网 http/https 链接；禁止本机/内网/回环地址。")
    direct, kind = _looks_like_direct(url)
    if direct:
        return _direct_result(url, kind)
    host = (urlparse(url).hostname or "").lower()
    if any(k in host for k in ("douyin", "iesdouyin", "snssdk", "amemv", "ixigua")):
        return _resolve_douyin(url)
    if any(k in host for k in ("bilibili", "b23.tv", "bilivideo")):
        return _resolve_bilibili(url)
    return _fail("这是网页不是媒体文件，无法直接解析为可播放的媒体直链", HINT_WEBPAGE)


# ----------------------------------------------------------------------------
# /api/media 的辅助
# ----------------------------------------------------------------------------
def _guess_content_type(url, upstream=""):
    p = urlparse(url)
    seg = p.path.rsplit("/", 1)[-1]
    ext = seg.rsplit(".", 1)[-1].lower() if "." in seg else ""
    if ext in CT_BY_EXT:
        return CT_BY_EXT[ext]
    if upstream:
        return upstream.split(";")[0].strip() or "application/octet-stream"
    return "application/octet-stream"


def _referer_for(url):
    host = (urlparse(url).hostname or "").lower()
    if any(k in host for k in ("douyin", "iesdouyin", "snssdk", "aweme", "amemv",
                               "douyinvod", "bytecdn", "ixigua", "toutiao")):
        return "https://www.douyin.com/"
    if any(k in host for k in ("bilibili", "bilivideo", "biliapi", "hdslb",
                               "upos", "akamaized", "bili")):
        return "https://www.bilibili.com/"
    p = urlparse(url)
    return "%s://%s/" % (p.scheme, p.netloc)


# ----------------------------------------------------------------------------
# /api/export 的辅助
# ----------------------------------------------------------------------------
def _safe_stem(filename):
    base = os.path.basename(str(filename or "")).strip()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "_", stem)
    return (stem or "export")[:60]


def _input_ext(filename):
    base = os.path.basename(str(filename or ""))
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            return ext
    return ".webm"


def _even(v, default=None):
    try:
        n = int(v)
    except Exception:
        return default
    if n < 2 or n > 7680:
        return default
    return n if n % 2 == 0 else n + 1


def _fps(v, default=30):
    try:
        n = float(v)
    except Exception:
        return "%g" % default
    if n <= 0 or n > 240:
        return "%g" % default
    return "%g" % n


def _bitrate(v):
    s = str(v or "").strip()
    return s if re.fullmatch(r"[0-9]{1,6}[kKmMgG]?", s) else "8M"


def _pick_encoder(candidates, avail, fallback):
    if avail:
        for c in candidates:
            if c in avail:
                return c, ""
    return fallback, ""


def _sibling_bin(ff, name):
    try:
        cand = os.path.join(os.path.dirname(ff),
                            name + (".exe" if _plat() == "windows" else ""))
        return cand if os.path.isfile(cand) else None
    except Exception:
        return None


def _probe_has_audio(ff, in_path):
    """返回 True/False；无法探测（没有 ffprobe）返回 None。"""
    ffprobe = _sibling_bin(ff, "ffprobe") or shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        p = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", in_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        return bool((p.stdout or b"").strip())
    except Exception:
        return None


def _ok_file(path, filename, cmd_display, notes):
    with open(path, "rb") as f:
        out = f.read()
    res = {"ok": True, "filename": filename,
           "dataBase64": base64.b64encode(out).decode("ascii"),
           "bytes": len(out), "cmd": cmd_display}
    if notes:
        res["note"] = "；".join(notes)
    return res


def _run_video(ff, td, in_path, data, stem, avail):
    p = _plat()
    container = str(data.get("container") or "mp4").lower()
    if container not in ("mp4", "mov", "webm"):
        container = "mp4"
    vcodec = str(data.get("vcodec") or "h264").lower()
    if vcodec not in ("h264", "hevc", "prores", "av1"):
        vcodec = "h264"
    notes = []
    if vcodec == "h264":
        if p == "mac":
            cands = ["h264_videotoolbox", "h264_nvenc", "libx264"]
        elif p == "windows":
            cands = ["h264_nvenc", "h264_videotoolbox", "libx264"]
        else:
            cands = ["h264_nvenc", "libx264"]
        venc, n = _pick_encoder(cands, avail, "libx264")
    elif vcodec == "hevc":
        cands = (["hevc_videotoolbox", "hevc_nvenc", "libx265"] if p == "mac"
                 else ["hevc_nvenc", "libx265"])
        venc, n = _pick_encoder(cands, avail, "libx265")
    elif vcodec == "prores":
        venc, n = "prores_ks", ""
        container = "mov"  # ProRes 容器强制 mov
    else:  # av1
        picked = next((c for c in ("libsvtav1", "libaom-av1")
                       if (not avail or c in avail)), None)
        if picked:
            venc, n = picked, ""
        else:
            venc, n = "libx264", "本机 FFmpeg 不含 libsvtav1/libaom-av1，AV1 已回退到 H.264（libx264）"
    if n:
        notes.append(n)

    ext = container
    out_path = os.path.join(td, "output." + ext)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", in_path]
    if vcodec == "prores":
        cmd += ["-c:v", "prores_ks", "-profile:v", "2"]
    else:
        cmd += ["-c:v", venc]
    w = _even(data.get("width"), None)
    h = _even(data.get("height"), None)
    if w and h:
        cmd += ["-vf", "scale=%d:%d:flags=lanczos" % (w, h)]
    if data.get("fps") is not None:
        cmd += ["-r", _fps(data.get("fps"))]
    cmd += ["-b:v", _bitrate(data.get("bitrate"))]

    acodec = str(data.get("acodec") or "").lower()
    ac_map = {"aac": "aac", "mp3": "libmp3lame", "wav": "pcm_s16le"}
    has_audio = _probe_has_audio(ff, in_path)
    if has_audio is False:
        cmd += ["-an"]
    else:
        aenc = ac_map.get(acodec) or {"mov": "aac", "mp4": "aac",
                                      "webm": "libopus"}.get(container, "aac")
        if avail and aenc not in avail and aenc != "aac":
            aenc = "aac"
            notes.append("所选音频编码器不可用，已回退到 aac")
        cmd += ["-c:a", aenc]
    if container == "mov":
        cmd += ["-f", "mov"]
    elif container == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += [out_path]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=TIMEOUT_TRANSCODE)
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        err = _scrub((proc.stderr or b"").decode("utf-8", "replace"), ff, td)[-600:]
        return _fail("FFmpeg 转码失败：%s" % (err or "未生成输出文件"),
                     "可尝试换用 H.264 + MP4，或降低分辨率/码率；并确认中间文件本身可播放。")
    return _ok_file(out_path, stem + "." + ext, _scrub_cmd(cmd, ff, td), notes)


def _run_audio(ff, td, in_path, data, stem):
    conf = {"mp3": ("libmp3lame", ".mp3"),
            "wav": ("pcm_s16le", ".wav"),
            "m4a": ("aac", ".m4a")}
    acodec = str(data.get("acodec") or "mp3").lower()
    if acodec not in conf:
        acodec = "mp3"
    aenc, ext = conf[acodec]
    out_path = os.path.join(td, "output" + ext)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", in_path,
           "-vn", "-c:a", aenc, out_path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=TIMEOUT_TRANSCODE)
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        err = _scrub((proc.stderr or b"").decode("utf-8", "replace"), ff, td)[-600:]
        return _fail("FFmpeg 抽取音频失败：%s" % (err or "未生成输出文件"),
                     "请确认中间文件含有音轨；或改用其它音频格式。")
    return _ok_file(out_path, stem + ext, _scrub_cmd(cmd, ff, td), [])


def _run_gif(ff, td, in_path, data, stem):
    w = _even(data.get("width"), 480)
    fps = _fps(data.get("fps"), 12)
    out_path = os.path.join(td, "output.gif")
    fc = ("[0:v] fps=%s,scale=%d:-2:flags=lanczos,split [a][b];"
          "[a] palettegen=stats_mode=diff [p];"
          "[b][p] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle") % (fps, w)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", in_path,
           "-filter_complex", fc, "-loop", "0", out_path]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=TIMEOUT_TRANSCODE)
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        err = _scrub((proc.stderr or b"").decode("utf-8", "replace"), ff, td)[-600:]
        return _fail("FFmpeg 生成 GIF 失败：%s" % (err or "未生成输出文件"),
                     "请确认中间文件含视频画面；或降低宽度/帧率后重试。")
    return _ok_file(out_path, stem + ".gif", _scrub_cmd(cmd, ff, td), [])



# ----------------------------------------------------------------------------
# /api/transcribe —— 音视频转字幕（含 VAD 静音区间，供「智能剪口播」使用）
# ----------------------------------------------------------------------------
_ASR_PROBE_CACHE = {"ts": 0, "ok": False}


def _asr_probe_local():
    """探测本机 faster-whisper / OpenAI 兼容转写服务（默认 9000），结果缓存 60 秒。"""
    import time
    now = time.time()
    if now - _ASR_PROBE_CACHE["ts"] < 60:
        return _ASR_PROBE_CACHE["ok"]
    ok = False
    for path in ("/v1/models", "/docs", "/"):
        try:
            req = urllib.request.Request(ASR_LOCAL_BASE + path,
                                         headers={"User-Agent": "manflows-local/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_ASR_PROBE) as r:
                if getattr(r, "status", 200) < 500:
                    ok = True
                    break
        except Exception:
            continue
    _ASR_PROBE_CACHE["ts"] = now
    _ASR_PROBE_CACHE["ok"] = ok
    return ok


def _asr_endpoint_ok(ep):
    """转写接口白名单：仅允许本机 9000 与公网地址，避免把本机服务当 SSRF 跳板。"""
    try:
        u = urlparse(str(ep or ""))
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https") or not host:
            return False
        if host in ("127.0.0.1", "localhost", "::1"):
            return (u.port or 80) == 9000
        ok, _why = is_allowed_target(str(ep))
        return ok
    except Exception:
        return False


def _extract_mono_wav(ff, in_path, td, clip_start, clip_duration):
    """从任意音视频里抽出 16kHz / 单声道 / s16le WAV，可只取片段对应的区间。"""
    out = os.path.join(td, "mono.wav")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
    cs = max(0.0, float(clip_start or 0))
    cd = max(0.0, float(clip_duration or 0))
    if cs > 0.02:
        cmd += ["-ss", "%.3f" % cs]
    cmd += ["-i", in_path]
    if cd > 0.05:
        cmd += ["-t", "%.3f" % cd]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=TIMEOUT_TRANSCODE)
    if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 44:
        err = _scrub((proc.stderr or b"").decode("utf-8", "replace"), ff, td)[-400:]
        raise RuntimeError("FFmpeg 抽取音轨失败：%s" % (err or "素材可能没有声音轨"))
    return out


def _detect_silences(ff, wav_path):
    """FFmpeg silencedetect 找出气口 / 长停顿，返回相对该音频开头的秒数区间。"""
    cmd = [ff, "-hide_banner", "-i", wav_path,
           "-af", "silencedetect=noise=-35dB:d=0.45", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=TIMEOUT_TRANSCODE)
        txt = (proc.stderr or b"").decode("utf-8", "replace")
    except Exception:
        return []
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", txt)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", txt)]
    out = []
    for i, st in enumerate(starts):
        en = ends[i] if i < len(ends) else (st + 0.5)
        if en > st:
            out.append({"start": round(st, 3), "end": round(en, 3),
                        "duration": round(en - st, 3)})
    return out[:600]


def _split_wav(wav_path, td, chunk_sec=ASR_CHUNK_SEC):
    """纯标准库按固定秒数切 16k/mono WAV，不依赖任何额外编码器。"""
    with wave.open(wav_path, "rb") as w:
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        total = w.getnframes()
        raw = w.readframes(total)
    size = rate * width * ch
    per = max(1, int(rate * chunk_sec)) * (width * ch)
    out = []
    idx = 0
    for off in range(0, len(raw), per):
        piece = raw[off:off + per]
        if not piece:
            break
        p = os.path.join(td, "chunk_%03d.wav" % idx)
        with wave.open(p, "wb") as o:
            o.setnchannels(ch); o.setsampwidth(width); o.setframerate(rate)
            o.writeframes(piece)
        out.append({"path": p, "start": off / float(size),
                    "duration": len(piece) / float(size)})
        idx += 1
    return out, total / float(rate or 16000)


def _post_asr(ep, key, model, language, chunk_path):
    """multipart/form-data 上传到 OpenAI 兼容 /v1/audio/transcriptions。"""
    boundary = "----ManflowsASR" + os.urandom(8).hex()
    parts = []
    for k, v in (("model", model or "whisper-1"),
                 ("response_format", "verbose_json"),
                 ("language", language or "zh")):
        if not v:
            continue
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode("utf-8"))
    with open(chunk_path, "rb") as f:
        filedata = f.read()
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                  "filename=\"chunk.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
                  % boundary).encode("utf-8") + filedata + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    headers = {"Content-Type": "multipart/form-data; boundary=" + boundary,
               "User-Agent": "manflows-local/1.0"}
    if key:
        headers["Authorization"] = "Bearer " + str(key)
    req = urllib.request.Request(ep, data=body, headers=headers, method="POST")
    try:
        with _build_opener(unverified=True).open(req, timeout=TIMEOUT_ASR) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise RuntimeError("转写接口 HTTP %d：%s" % (e.code, detail.strip()[:200]))
    except Exception as e:
        raise RuntimeError("连接转写接口失败：%s" % _short(e))
    try:
        j = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise RuntimeError("转写接口返回的不是 JSON：%s" % raw[:120].decode("utf-8", "replace"))
    segs = j.get("segments")
    if not isinstance(segs, list) or not segs:
        segs = ((j.get("result") or {}).get("segments")
                if isinstance(j.get("result"), dict) else None)
    if not isinstance(segs, list) or not segs:
        txt = str(j.get("text") or "").strip()
        return [{"start": 0.0, "end": 0.0, "text": txt}] if txt else []
    out = []
    for x in segs:
        if not isinstance(x, dict):
            continue
        t = str(x.get("text") or "").strip()
        if not t:
            continue
        st = float(x.get("start") or 0)
        en = float(x.get("end") or (st + 2))
        out.append({"start": st, "end": en, "text": t})
    return out


def api_transcribe(body):
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except Exception as e:
        return _fail("请求体不是合法 JSON：%s" % _short(e))
    if not isinstance(data, dict):
        return _fail("请求体必须是 JSON 对象")

    info = detect_ffmpeg()
    if not info["found"]:
        return {"ok": False, "reason": "本机未找到 FFmpeg，无法从视频里抽取音轨",
                "guide": FFMPEG_GUIDE,
                "hint": "请安装 FFmpeg（%s）后重启本服务；点一次『导出成片』也会自动尝试安装。" %
                        FFMPEG_GUIDE.get(_plat(), "")}
    ff = info["path"]

    # 选后端：① 本机 faster-whisper ② 第三方 OpenAI 兼容转写接口 ③ 明确安装指引
    local_ok = _asr_probe_local()
    third = str(data.get("endpoint") or "").strip()
    if local_ok:
        ep = ASR_LOCAL_BASE + "/v1/audio/transcriptions"
        key = ""
        model = "whisper-1"
        engine = "本机 faster-whisper（127.0.0.1:9000）"
    elif third and _asr_endpoint_ok(third):
        ep = third
        key = str(data.get("key") or "").strip()
        model = str(data.get("model") or "whisper-1").strip()
        engine = "第三方 OpenAI 兼容转写接口（本机服务转发）"
    elif third and not _asr_endpoint_ok(third):
        return _fail("转写接口地址不被允许：%s" % third[:120],
                     "只允许公网 https/http 地址或本机 127.0.0.1:9000；禁止内网/元数据地址。")
    else:
        return {"ok": False, "reason": "没有可用的语音转写后端",
                "hint": ASR_INSTALL_GUIDE,
                "engine": "none", "segments": [], "silences": []}

    # 取素材字节：本地 Blob 的 base64，或公网直链（由本服务下载）
    b64 = str(data.get("dataBase64") or "")
    if "," in b64 and b64.strip().lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    url = str(data.get("url") or "").strip()
    fname = _safe_stem(str(data.get("filename") or "audio.wav"))
    clip_start = max(0.0, float(data.get("clipStart") or 0))
    clip_duration = max(0.0, float(data.get("clipDuration") or 0))

    with tempfile.TemporaryDirectory(prefix="manflows-asr-") as td:
        in_path = os.path.join(td, "input" + (fname and ("." + (fname.rsplit(".", 1)[-1] if "." in fname else "mp4")) or ".mp4"))
        if b64:
            try:
                raw = base64.b64decode(b64, validate=False)
            except Exception as e:
                return _fail("dataBase64 解码失败：%s" % _short(e))
            if not raw:
                return _fail("dataBase64 为空，没有可识别的音视频数据")
            with open(in_path, "wb") as f:
                f.write(raw)
        elif url:
            ok, why = is_allowed_target(url)
            if not ok:
                return _fail("素材地址不被允许：%s" % why)
            try:
                resp = _open_url(url, headers={"User-Agent": UA_DESKTOP,
                                               "Referer": _referer_for(url)},
                                 timeout=TIMEOUT_MEDIA)
                with open(in_path, "wb") as f:
                    while True:
                        buf = resp.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
            except Exception as e:
                return _fail("下载素材失败：%s" % _short(e),
                             "请改用本地导入的素材，或确认直链可访问。")
        else:
            return _fail("缺少素材：请提供 dataBase64 或 url",
                         "本地导入的素材会由网页把 Blob 转成 base64 交给本服务。")

        try:
            wav = _extract_mono_wav(ff, in_path, td, clip_start, clip_duration)
            chunks, duration = _split_wav(wav, td)
            silences = _detect_silences(ff, wav)
        except subprocess.TimeoutExpired:
            return _fail("抽取音轨超时（超过 10 分钟）", "请换短一点的素材再试。")
        except Exception as e:
            return _fail(_short(e), "请确认素材含有声音轨、且 ffmpeg 可读取该格式。")

        segments = []
        errors = []
        for i, c in enumerate(chunks):
            try:
                got = _post_asr(ep, key, model,
                                str(data.get("language") or "zh"), c["path"])
            except Exception as e:
                errors.append("第 %d 段：%s" % (i + 1, _short(e)))
                if i == 0:
                    break
                continue
            for g in got:
                st = c["start"] + float(g.get("start") or 0) + clip_start
                en = c["start"] + float(g.get("end") or 0) + clip_start
                if en <= st:
                    en = st + max(1.0, len(str(g.get("text") or "")) * 0.18)
                segments.append({"start": round(st, 3), "end": round(en, 3),
                                 "text": str(g.get("text") or "").strip()})
        if errors and not segments:
            return {"ok": False, "reason": "语音转写失败：" + errors[0],
                    "engine": engine, "hint": ASR_INSTALL_GUIDE,
                    "segments": [], "silences": []}

        for z in silences:
            z["start"] = round(z["start"] + clip_start, 3)
            z["end"] = round(z["end"] + clip_start, 3)
        segments.sort(key=lambda x: x["start"])
        return {"ok": True, "engine": engine, "chunks": len(chunks),
                "duration": round(duration, 2),
                "segments": segments[:2000], "silences": silences,
                "partialErrors": errors[:5],
                "hint": ("已由本机服务抽成 16k 单声道音频并分 %d 段识别，"
                         "时间码已合并；未调用任何云端 VIP 功能。" % len(chunks))}


def api_export(body):
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except Exception as e:
        return _fail("请求体不是合法 JSON：%s" % _short(e),
                     "请以 application/json 发送参数。")
    if not isinstance(data, dict):
        data = {}

    info = detect_ffmpeg()
    if not info["found"]:
        cur = FFMPEG_GUIDE.get(_plat(), "")
        return {"ok": False, "reason": "本机未找到 FFmpeg，无法转码导出",
                "guide": FFMPEG_GUIDE, "url": cur,
                "hint": "请安装 FFmpeg（%s）后重启本服务，或服务会自动尝试安装。" % cur}

    ff = info["path"]
    avail = set(info.get("encoders") or [])
    action = str(data.get("action") or "export").lower()

    b64 = data.get("dataBase64") or ""
    if isinstance(b64, str) and "," in b64 and b64.strip().lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception as e:
        return _fail("dataBase64 解码失败：%s" % _short(e),
                     "请确认传入的是纯 base64 字符串。")
    if not raw or len(raw) < 16:
        return _fail("dataBase64 为空或过小，没有可转码的媒体数据",
                     "请先在浏览器里录制/生成中间文件再导出。")

    fname = str(data.get("filename") or "export.mp4")
    stem = _safe_stem(fname)
    with tempfile.TemporaryDirectory(prefix="manflows-") as td:
        in_path = os.path.join(td, "input" + _input_ext(fname))
        with open(in_path, "wb") as f:
            f.write(raw)
        try:
            if action == "audio":
                result = _run_audio(ff, td, in_path, data, stem)
            elif action == "gif":
                result = _run_gif(ff, td, in_path, data, stem)
            else:
                result = _run_video(ff, td, in_path, data, stem, avail)
        except subprocess.TimeoutExpired:
            return _fail("FFmpeg 转码超时（超过 10 分钟）",
                         "请缩短片段、降低分辨率/码率后重试。")
        except Exception as e:
            return _fail("转码过程出错：%s" % _short(e),
                         "请确认录制的中间文件可正常播放，或降低导出参数后重试。")
        return result


# ----------------------------------------------------------------------------
# HTTP Handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ManflowsLocal/%s" % VERSION

    def _cors(self):
        origin = self.headers.get("Origin")
        return {
            "Access-Control-Allow-Origin": origin if origin else "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Range, *",
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Expose-Headers":
                "Content-Length, Content-Range, Accept-Ranges, Content-Disposition",
            "Vary": "Origin",
        }

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _send_preflight(self):
        self.send_response(204)
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return b"", _fail("Content-Length 非法")
        if length <= 0:
            return b"", None
        if length > MAX_BODY:
            self.close_connection = True
            return b"", _fail(
                "请求体过大（约 %d MB），超过 %d MB 上限" %
                (length // (1024 * 1024), MAX_BODY // (1024 * 1024)),
                "请缩短片段或降低录制分辨率/码率后重试。")
        try:
            return self.rfile.read(length), None
        except Exception as e:
            return b"", _fail("读取请求体失败：%s" % _short(e))

    def _handle_media(self, u):
        u = (u or "").strip()
        if not u:
            self._send_json(_fail("缺少 u 参数", "用法：/api/media?u=<urlencoded 直链>"))
            return
        ok, why = is_allowed_target(u)
        if not ok:
            self._send_json(_fail("被代理地址不被允许：%s" % why,
                                  "仅允许公网 http/https 直链；禁止本机/内网/回环地址（防 SSRF 与自激）。"))
            return
        headers = {"User-Agent": UA_DESKTOP, "Accept": "*/*",
                   "Referer": _referer_for(u)}
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        try:
            resp = _open_url(u, headers=headers, timeout=TIMEOUT_MEDIA)
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                e.close()
            except Exception:
                pass
            if code in (401, 403):
                self._send_json(_fail(
                    "上游返回 HTTP %s，可能是防盗链（Referer/Cookie 校验）或直链已过期" % code,
                    "请重新解析获取新的直链，或把来源平台的分享链接粘贴进解析框重试。"))
            elif code == 404:
                self._send_json(_fail("上游返回 HTTP 404，媒体直链不存在或已失效",
                                      "请重新解析或粘贴有效的 .mp4/.mp3 直链。"))
            else:
                self._send_json(_fail("上游返回 HTTP %s，无法获取媒体" % code,
                                      "请检查直链是否有效，或稍后重试。"))
            return
        except Exception as e:
            self._send_json(_fail("连接上游媒体失败：%s" % _short(e),
                                  "可能是网络不通、超时或直链失效。"))
            return
        try:
            status = getattr(resp, "status", None) or resp.getcode() or 200
            if status >= 400:
                self._send_json(_fail("上游返回 HTTP %s" % status,
                                      "媒体直链可能已失效或存在防盗链。"))
                return
            ctype = _guess_content_type(u, resp.headers.get("Content-Type"))
            clen = resp.headers.get("Content-Length")
            crange = resp.headers.get("Content-Range")
            self.send_response(status)
            for k, v in self._cors().items():
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if crange:
                self.send_header("Content-Range", crange)
            if clen:
                self.send_header("Content-Length", clen)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            written = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                written += len(chunk)
            if clen:
                try:
                    if written != int(clen):
                        self.close_connection = True
                except ValueError:
                    self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True  # 客户端断开，正常结束，不再写 JSON
        except Exception:
            self.close_connection = True  # 已经开始发送二进制流，避免再塞 JSON
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def do_OPTIONS(self):
        self._send_preflight()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path, q = parsed.path, parse_qs(parsed.query)
            if path == "/api/ping":
                self._send_json(api_ping())
            elif path == "/api/ffmpeg-install":
                self._send_json(api_ffmpeg_install())
            elif path == "/api/guide":
                self._send_json(api_guide())
            elif path == "/api/resolve":
                self._send_json(api_resolve(q.get("url", [""])[0]))
            elif path == "/api/media":
                self._handle_media(q.get("u", [""])[0])
            elif path == "/":
                self._send_json({"ok": True, "service": "漫流 AI 剪辑台 · 本机服务",
                                 "version": VERSION, "port": PORT,
                                 "hint": "服务已就绪；可用接口见 /api/guide"})
            else:
                self._send_json(_fail("未知接口：%s" % path,
                                      "可用接口：/api/ping、/api/guide、/api/ffmpeg-install、"
                                      "/api/resolve、/api/media、/api/export、/api/transcribe"))
        except Exception as e:
            try:
                self._send_json(_fail("服务内部错误：%s" % _short(e),
                                      "请重启本服务后重试；若持续出现请把此信息反馈给开发者。"))
            except Exception:
                self.close_connection = True

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            body, err = self._read_body()
            if err is not None:
                self._send_json(err)
                return
            if path == "/api/resolve":
                try:
                    data = json.loads(body.decode("utf-8")) if body else {}
                except Exception:
                    data = {}
                url = data.get("url", "") if isinstance(data, dict) else ""
                self._send_json(api_resolve(url))
            elif path == "/api/export":
                self._send_json(api_export(body))
            elif path == "/api/transcribe":
                self._send_json(api_transcribe(body))
            else:
                self._send_json(_fail("未知接口：%s" % path,
                                      "可用的 POST 接口：/api/resolve、/api/export、/api/transcribe"))
        except Exception as e:
            try:
                self._send_json(_fail("服务内部错误：%s" % _short(e),
                                      "请重启本服务后重试；若持续出现请把此信息反馈给开发者。"))
            except Exception:
                self.close_connection = True

    def log_message(self, fmt, *args):
        # 精简日志：只打印方法与去掉查询串的路径，避免刷屏与泄露直链
        try:
            path = (self.path or "").split("?", 1)[0]
            sys.stderr.write("%s %s %s\n" %
                             (self.log_date_time_string(), self.command, path))
        except Exception:
            pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def main():
    info = detect_ffmpeg()
    print("=" * 62)
    print("  漫流 AI 剪辑台 · 本机服务  v%s" % VERSION)
    print("  平台：%s %s | Python %s" % (_plat(), _arch(), _python_ver()))
    print("  监听：http://127.0.0.1:%d  （仅本机回环，不对外网开放）" % PORT)
    if info["found"]:
        print("  FFmpeg：已就绪（%s）" % (info["version"] or info["path"]))
    else:
        print("  FFmpeg：未检测到 —— 点『导出成片』时会自动尝试安装")
        print("  安装指引：%s" % FFMPEG_GUIDE.get(_plat(), ""))
    print("  浏览器里点『导出成片』会自动探测本服务；保持此窗口开着")
    print("  停止服务：按 Ctrl+C 或关闭本窗口")
    print("=" * 62, flush=True)
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("")
        print("【启动失败】无法监听 127.0.0.1:%d，端口可能已被其它程序占用（或没有权限）。" % PORT)
        print("  若已经有另一个『漫流本机服务』在运行，请先关闭它的窗口再重新启动。")
        print("  详细错误：%s" % _short(e))
        sys.exit(1)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n本机服务已停止。")
    finally:
        try:
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
