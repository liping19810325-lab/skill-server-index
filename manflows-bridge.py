#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
漫流 MANFLOWS · 本机启停桥接服务 v1.0
=========================================================
作用：浏览器沙箱里不能直接运行本机程序，所以由这个只监听
      127.0.0.1 的小服务替网页「打开 / 关闭」llama-server、Ollama、
      llama.cpp、LM Studio、ComfyUI 等本机程序。

支持选这些当启动目标（在网页里点「浏览…」选文件，或直接粘贴完整路径）：
      .exe / .bat / .cmd / .py / .sh / macOS .app / 纯命令（如 ollama serve）
      目录也可以：填 ComfyUI 的启动 bat 或 python main.py 所在目录均可。
      程序路径、进程名只存在你本机，网页源码里一个都没有。

用法：
  1) 双击同目录的「启动漫流桥接.bat」（或命令行运行 python manflows-bridge.py）
  2) 保持这个黑窗口开着，网页里就能「浏览…」选程序、点「启动 / 停止」
  3) 首次运行会在同目录生成 manflows-bridge.json，配置只存你本机

接口（同源 http 页面调用）：
  GET  /api/ping                    探测桥是否在线
  POST /api/pick   {target}         弹出本机文件选择框，返回所选程序绝对路径
  GET  /api/config?target=llama     读取某服务的启动配置
  POST /api/config                  保存配置 {target,path,args,cwd,health,process_names,show_window}
  GET  /api/status?target=llama     查询运行状态 / 监听 PID
  POST /api/start   {target}        启动（可附带 config 一起保存）
  POST /api/stop    {target}        停止（杀进程树 + 端口监听进程 + 进程名）
  GET  /api/logs                    最近操作日志，排错用

安全：只绑定 127.0.0.1；所有会启动进程的接口都校验 Origin 白名单
      （本机 localhost / 127.0.0.1、manflows.cn、*.github.io，可在
       manflows-bridge.json 的 allowed_origins 里追加自己的域名）。
      这个窗口关掉 = 网页的启停按钮失效，但已启动的 llama / ComfyUI 不受影响。

说明：网页是静态页，本身不含任何 API Key、模型名或本机路径；
      路径全部由你在本机浏览器里填写并保存在 manflows-bridge.json / localStorage。
"""

import os
import re
import sys
import json
import time
import shlex
import socket
import signal
import subprocess
import threading
import urllib.parse
import urllib.request
import urllib.error
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "manflows-bridge.json")
IS_WIN = os.name == "nt"

# 按用户提供的实际环境预填，仅为「开箱即用」的默认值，网页里可随时改。
# 这里只放本机启动脚本路径，不含任何 API Key / 模型名 / 接口地址凭证。
DEFAULT_CONFIG = {
    "port": 8787,
    "wait_seconds": 90,
    "allowed_origins": [],
    "services": {
        "llama": {
            "path": r"C:\Llama_server\start_server.bat",
            "args": "",
            "cwd": "",
            "health": "http://127.0.0.1:8080/health",
            "show_window": True,
            "process_names": ["llama-server.exe"],
            "note": "llama.cpp：start_server.bat（--port 8080，末尾 pause）；Ollama：path 填 ollama、args 填 serve、health 改 http://127.0.0.1:11434/api/tags"
        },
        "comfyui": {
            "path": "",
            "args": "",
            "cwd": "",
            "health": "http://127.0.0.1:8188/system_stats",
            "show_window": True,
            "process_names": [],
            "note": "填入 ComfyUI 的启动 bat / python.exe，例如 D:\\ComfyUI\\run_nvidia_gpu.bat；不填则用不到本服务"
        }
    }
}

CONFIG = {}
PROCS = {}                      # target -> subprocess.Popen（由本桥启动的进程）
LOGS = deque(maxlen=200)
LOCK = threading.Lock()


def log(msg):
    line = time.strftime("%H:%M:%S") + " " + str(msg)
    LOGS.append(line)
    try:
        print(line, flush=True)
    except Exception:
        pass


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user.get("port"), int):
                cfg["port"] = user["port"]
            if isinstance(user.get("wait_seconds"), int):
                cfg["wait_seconds"] = user["wait_seconds"]
            if isinstance(user.get("allowed_origins"), list):
                cfg["allowed_origins"] = user["allowed_origins"]
            for key, val in (user.get("services") or {}).items():
                if key in cfg["services"] and isinstance(val, dict):
                    for k in ("path", "args", "cwd", "health", "show_window",
                              "process_names", "note"):
                        if k in val:
                            cfg["services"][key][k] = val[k]
        except Exception as e:
            log("配置文件读取失败（已用默认值）：%s" % e)
    return cfg


def save_config():
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def origin_allowed(origin):
    if not origin:
        return True                      # curl / 同源 / 无 Origin 的本地调用
    try:
        u = urllib.parse.urlparse(origin)
        host = (u.hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return True
        if host == "manflows.cn" or host.endswith(".manflows.cn"):
            return True
        if host.endswith(".github.io"):
            return True
        for extra in CONFIG.get("allowed_origins") or []:
            if extra and (host == extra.lower() or origin.startswith(extra)):
                return True
    except Exception:
        pass
    return False


def pick_file_cli():
    """子进程模式（--pick）：Tk 必须在主线程创建，所以单独起一个进程弹选择框。
    优先 tkinter；有些 Python 发行版不带 tkinter，此时退回系统原生对话框：
      Windows -> PowerShell + System.Windows.Forms.OpenFileDialog
      macOS   -> osascript choose file
    都不行时返回空串，网页会提示直接粘贴路径（功能不受影响）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askopenfilename(
            title="漫流：选择要由网页启动 / 关闭的程序",
            filetypes=[("程序与启动脚本", "*.exe *.bat *.cmd *.py *.sh"),
                       ("所有文件", "*.*")])
        root.destroy()
        print(p or "", flush=True)
        return
    except Exception:
        pass

    if IS_WIN:
        tmp = os.path.join(os.environ.get("TEMP") or BASE_DIR, "manflows_pick.txt")
        try:
            os.remove(tmp)
        except Exception:
            pass
        ps = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title='MANFLOWS - choose the program to start or stop';"
            "$d.Filter='Programs and scripts|*.exe;*.bat;*.cmd;*.py;*.sh|All files|*.*';"
            "$d.CheckFileExists=$true;"
            "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
            "$p=Join-Path $env:TEMP 'manflows_pick.txt';"
            "[System.IO.File]::WriteAllText($p,$d.FileName,[System.Text.Encoding]::UTF8)}"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                           timeout=120, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            if os.path.isfile(tmp):
                with open(tmp, "r", encoding="utf-8-sig") as f:
                    print(f.read().strip(), flush=True)
                os.remove(tmp)
                return
        except Exception as e:
            print("windows pick failed: %s" % e, file=sys.stderr)
    elif sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["osascript", "-e",
                 "POSIX path of (choose file with prompt \"MANFLOWS - choose the program\")"],
                timeout=120, stderr=subprocess.DEVNULL).decode("utf-8", "ignore").strip()
            print(out, flush=True)
            return
        except Exception as e:
            print("mac pick failed: %s" % e, file=sys.stderr)
    print("", flush=True)


def pick_file():
    try:
        out = subprocess.check_output(
            [sys.executable, os.path.abspath(__file__), "--pick"],
            cwd=BASE_DIR, timeout=120, stderr=subprocess.DEVNULL
        ).decode("utf-8", "ignore").strip()
        return out
    except Exception as e:
        log("文件选择框打开失败（可直接粘贴路径）：%s" % e)
        return ""


def health_port(url):
    try:
        u = urllib.parse.urlparse(url)
        return u.port or (443 if u.scheme == "https" else 80)
    except Exception:
        return None


# 这些名字太通用，绝不能拿来「按进程名停止」——否则会把桥自己、静态服务、
# 编辑器等无关进程一起杀掉。用户要用 python main.py 时靠 PID + 端口停止即可。
GENERIC_PROCESS_NAMES = {
    "python.exe", "python", "python3", "python3.exe", "pythonw.exe",
    "node.exe", "node", "cmd.exe", "conhost.exe", "powershell.exe",
    "pwsh.exe", "bash", "sh", "zsh", "explorer.exe", "svchost.exe"
}


def service_names(svc):
    """停止时按进程名兜底：覆盖「程序不监听端口」或「用户手动双击启动」的情况。"""
    names = list(svc.get("process_names") or [])
    p = (svc.get("path") or "").strip()
    if p:
        base = os.path.basename(p.rstrip("/\\"))
        low = base.lower()
        if low.endswith(".exe"):
            names.append(base)
        elif low.endswith(".app"):
            names.append(base[:-4])
        elif (not os.path.isabs(p) and " " not in p
              and not low.endswith((".bat", ".cmd", ".py", ".sh"))):
            names.append(base if not IS_WIN else
                         (base if base.lower().endswith(".exe") else base + ".exe"))
    return list(dict.fromkeys(
        n for n in names if n and n.lower() not in GENERIC_PROCESS_NAMES))


def find_name_pids(names):
    pids = set()
    names = [n for n in (names or []) if n]
    if not names:
        return pids
    try:
        if IS_WIN:
            out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"],
                                          stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
            low = [n.lower() for n in names]
            low += [n.lower().replace(".exe", "") for n in names]
            for line in out.splitlines():
                parts = [x.strip('" ') for x in line.split('","')]
                if len(parts) < 2:
                    continue
                exe = parts[0].strip('"').lower()
                if exe in low:
                    try:
                        pids.add(int(parts[1].strip('"')))
                    except Exception:
                        pass
        else:
            for n in names:
                try:
                    out = subprocess.check_output(["pgrep", "-f", n],
                                                  stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
                    pids.update(int(x) for x in out.split() if x.isdigit())
                except Exception:
                    pass
    except Exception:
        pass
    return pids


def probe(url):
    """返回 (服务是否已在监听, HTTP 状态码)。
    HTTPError 也算「已监听」：llama.cpp 加载模型时 /health 会返回 503。"""
    if not url:
        return False, None
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "manflows-bridge/" + VERSION})
        with urllib.request.urlopen(req, timeout=1.2) as r:
            return True, r.status
    except urllib.error.HTTPError as e:
        return True, e.code
    except Exception:
        pass
    try:
        u = urllib.parse.urlparse(url)
        s = socket.create_connection((u.hostname or "127.0.0.1",
                                      u.port or (443 if u.scheme == "https" else 80)), 0.6)
        s.close()
        return True, "tcp"
    except Exception:
        return False, None


def find_listen_pids(port):
    """按端口找 LISTENING 的 PID，覆盖「用户自己双击 bat 启动、桥没记录 PID」的情况。"""
    pids = set()
    if not port:
        return pids
    try:
        if IS_WIN:
            out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                          stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
            rx = re.compile(r"^\s*TCP\s+\S+:%d\s+\S+\s+LISTENING\s+(\d+)" % port,
                            re.I | re.M)
            pids = {int(x) for x in rx.findall(out)}
        else:
            out = subprocess.check_output(
                ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
            pids = {int(x) for x in out.split() if x.isdigit()}
    except Exception:
        pass
    return pids


def kill_pid(pid):
    try:
        if IS_WIN:
            # /T 杀整棵进程树：cmd.exe -> llama-server.exe / python.exe
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        log("已终止进程树 PID=%s" % pid)
        return True
    except Exception as e:
        log("终止 PID=%s 失败：%s" % (pid, e))
        return False


def get_service(target):
    svc = CONFIG["services"].get(target)
    if not svc:
        raise ValueError("未知服务：%s（可用：llama / comfyui）" % target)
    return svc


def merge_config(target, body):
    """允许 start/stop/config 时顺带保存网页里刚选的路径，避免用户忘记点保存。"""
    payload = body.get("config") if isinstance(body.get("config"), dict) else body
    svc = get_service(target)
    changed = False
    for k in ("path", "args", "cwd", "health", "show_window", "process_names"):
        if k in payload:
            svc[k] = payload[k]
            changed = True
    if changed:
        save_config()


# 用户只选文件夹时，尝试自动找到常见的启动文件
START_FILE_CANDIDATES = [
    "start_server.bat", "run_nvidia_gpu.bat", "run_cpu.bat", "start.bat",
    "run.bat", "main.py", "llama-server.exe", "ollama.exe", "ComfyUI.exe"
]


def resolve_start_path(path):
    """返回 (实际要执行的路径, 工作目录)。目录时自动探测常见启动文件。"""
    if os.path.isdir(path):
        for c in START_FILE_CANDIDATES:
            fp = os.path.join(path, c)
            if os.path.isfile(fp):
                return fp, path
        raise ValueError(
            "选中的是文件夹，但里面没找到常见启动文件（start_server.bat / "
            "run_nvidia_gpu.bat / main.py / *.exe）。请直接选到具体的启动程序文件，"
            "或把启动脚本放进该文件夹。")
    cwd = os.path.dirname(path) if (os.path.isabs(path) and os.path.isfile(path)) else BASE_DIR
    return path, cwd


def is_running(target):
    """三条线索任一命中就算在运行：健康检查端口、本桥启动的进程、同名进程。
    这样纯 GUI 程序（不监听端口）也能正确显示「运行中 / 已停止」。"""
    svc = get_service(target)
    health = (svc.get("health") or "").strip()
    ok, code = probe(health) if health else (False, None)
    p = PROCS.get(target)
    managed = bool(p and p.poll() is None)
    names = service_names(svc)
    name_pids = sorted(find_name_pids(names)) if names else []
    listen_pids = sorted(find_listen_pids(health_port(health))) if (health and ok) else []
    return {
        "running": bool(ok or managed or name_pids),
        "healthUp": ok, "healthCode": code, "managed": managed,
        "pid": p.pid if managed else None,
        "listenPids": listen_pids, "namePids": name_pids, "names": names
    }


def status_service(target):
    svc = get_service(target)
    st = is_running(target)
    st.update({
        "ok": True, "target": target,
        "path": svc.get("path", ""), "cwd": svc.get("cwd", ""),
        "args": svc.get("args", ""), "health": svc.get("health", ""),
        "processNames": svc.get("process_names") or []
    })
    return st


def start_service(target, body=None):
    svc = get_service(target)
    if body:
        merge_config(target, body)
    st = is_running(target)
    if st["running"]:
        return {"already_running": True, "running": True,
                "message": "程序已经在运行，未重复启动（避免重复占用显存）。"}
    p = PROCS.get(target)
    if p and p.poll() is None:
        return {"starting": True, "pid": p.pid,
                "message": "启动程序还在加载，请稍后点「刷新状态」。"}

    path = (svc.get("path") or "").strip()
    if not path:
        raise ValueError("还没选启动程序。请点「浏览…」选择，或粘贴 .exe / .bat / .cmd / .app 的完整路径。")
    try:
        args = shlex.split(svc.get("args") or "", posix=not IS_WIN)
    except Exception:
        args = []
    cwd = (svc.get("cwd") or "").strip()
    real_path, auto_cwd = resolve_start_path(path)
    if not cwd:
        cwd = auto_cwd
    ext = os.path.splitext(real_path)[1].lower()

    if IS_WIN:
        if ext in (".bat", ".cmd") or not os.path.isfile(real_path):
            cmd = ["cmd.exe", "/c", real_path] + args   # 也覆盖 "ollama" 这类纯命令
        else:
            cmd = [real_path] + args
        flags = 0x00000010 if svc.get("show_window", True) else 0x08000000
        proc = subprocess.Popen(cmd, cwd=cwd, creationflags=flags)
    else:
        if real_path.lower().endswith(".app") and os.path.isdir(real_path):
            cmd = ["open", "-a", real_path] + args
        elif os.path.isfile(real_path):
            if os.access(real_path, os.X_OK):
                cmd = [real_path] + args
            elif ext == ".py":
                cmd = [sys.executable, real_path] + args
            else:
                cmd = ["/bin/bash", real_path] + args
        else:
            cmd = shlex.split(real_path, posix=True) + args
        proc = subprocess.Popen(cmd, cwd=cwd, start_new_session=True)

    PROCS[target] = proc
    log("启动 %s：%s  (cwd=%s, pid=%s)" % (target, " ".join(cmd), cwd, proc.pid))

    health = (svc.get("health") or "").strip()
    if not health:
        time.sleep(0.6)
        alive = proc.poll() is None
        return {"running": alive, "pid": proc.pid,
                "message": "已打开程序（未配置健康检查地址，按进程存活判断状态）。"
                           if alive else "程序打开后立即退出，请检查路径与启动参数。"}

    deadline = time.time() + int(CONFIG.get("wait_seconds", 90))
    while time.time() < deadline:
        time.sleep(1)
        ok, code = probe(health)
        if ok:
            log("%s 已就绪（health=%s）" % (target, code))
            return {"running": True, "pid": proc.pid, "healthCode": code,
                    "message": "已启动，服务端口就绪。"}
        if proc.poll() is not None:
            raise RuntimeError(
                "启动程序已退出（退出码 %s）。请检查：① 程序 / .bat 路径是否正确；"
                "② .bat 里用了相对路径时工作目录是否为它所在目录；"
                "③ 模型文件、显存是否满足。桥接黑窗口里能看到程序输出。"
                % proc.returncode)
    return {"starting": True, "pid": proc.pid,
            "message": "已启动，但端口还没就绪（模型加载中）；点「刷新状态」继续观察。"}


def stop_service(target, body=None):
    svc = get_service(target)
    if body:
        merge_config(target, body)
    st = is_running(target)
    candidates = set()
    p = PROCS.get(target)
    if p and p.poll() is None:
        candidates.add(p.pid)
    candidates.update(st["listenPids"])
    candidates.update(st["namePids"])
    # 绝不杀桥自己或它的父进程
    protected = {os.getpid()}
    try:
        protected.add(os.getppid())
    except Exception:
        pass
    candidates = {x for x in candidates if x not in protected}
    killed = [pid for pid in sorted(candidates) if kill_pid(pid)]
    PROCS.pop(target, None)
    time.sleep(0.9)
    after = is_running(target)
    if after["running"]:
        msg = ("已终止 %d 个进程，但程序似乎仍在运行（可能是另一个实例），"
               "请手动关闭它的窗口。" % len(killed)) if killed else \
              "没找到可关闭的进程；如果是手动打开的，请直接关掉它的窗口。"
        log("关闭 %s 后仍在运行" % target)
    else:
        msg = "已关闭，程序与服务端口都已释放。" if killed else "程序本来就没在运行。"
        log("关闭 %s 完成" % target)
    return {"stopped": not after["running"], "killed": killed,
            "healthStillUp": after["healthUp"], "stillRunning": after["running"],
            "message": msg}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                              # 用自带日志，避免刷屏

    def _cors(self):
        origin = self.headers.get("Origin", "")
        self.send_header("Access-Control-Allow-Origin", origin if origin else "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Manflows-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _guard(self):
        if not origin_allowed(self.headers.get("Origin", "")):
            self._send({"ok": False,
                        "error": "来源不被允许。请在本机 http://127.0.0.1 打开漫流页面，"
                                 "或把当前域名加进 manflows-bridge.json 的 allowed_origins。"}, 403)
            return False
        return True

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        target = (q.get("target", ["llama"])[0] or "llama").strip()
        path = u.path.rstrip("/") or "/"
        if path == "/api/ping":
            return self._send({"ok": True, "name": "manflows-bridge",
                               "version": VERSION, "os": "windows" if IS_WIN else "posix",
                               "port": CONFIG.get("port")})
        if not self._guard():
            return
        try:
            if path == "/api/config":
                svc = get_service(target)
                return self._send({"ok": True, "target": target, "config": svc})
            if path == "/api/status":
                return self._send(status_service(target))
            if path == "/api/logs":
                return self._send({"ok": True, "logs": list(LOGS)})
            return self._send({"ok": False, "error": "未知接口：" + path}, 404)
        except Exception as e:
            log("GET %s 出错：%s" % (path, e))
            return self._send({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        if not self._guard():
            return
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(n) if n > 0 else b"{}"
            body = json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception as e:
            return self._send({"ok": False, "error": "请求体不是合法 JSON：" + str(e)}, 400)
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        target = (body.get("target") or "llama").strip()
        if path == "/api/pick":
            # 弹窗可能要等用户操作几十秒，不放进全局锁，避免挡住状态查询
            picked = pick_file()
            if picked:
                get_service(target)["path"] = picked
                get_service(target)["cwd"] = ""     # 让桥自动用程序所在目录
                save_config()
                log("%s 已选定程序：%s" % (target, picked))
                return self._send({"ok": True, "target": target, "path": picked,
                                   "config": get_service(target),
                                   "message": "已选定程序，可直接点「启动」。"})
            return self._send({"ok": False,
                               "error": "没有选择文件，或本机文件选择框不可用（无桌面会话时会出现）。"
                                        "请直接把程序完整路径粘贴到输入框。"}, 400)
        try:
            with LOCK:
                if path == "/api/config":
                    merge_config(target, {"config": body})
                    return self._send({"ok": True, "target": target,
                                       "config": get_service(target),
                                       "message": "设置已保存到本机 manflows-bridge.json。"})
                if path == "/api/start":
                    r = start_service(target, body)
                    r["ok"] = True
                    return self._send(r)
                if path == "/api/stop":
                    r = stop_service(target, body)
                    r["ok"] = True
                    return self._send(r)
                return self._send({"ok": False, "error": "未知接口：" + path}, 404)
        except Exception as e:
            log("POST %s 出错：%s" % (path, e))
            return self._send({"ok": False, "error": str(e)}, 500)


def main():
    global CONFIG
    CONFIG = load_config()
    if not os.path.isfile(CONFIG_PATH):
        save_config()
    port = int(CONFIG.get("port", 8787))
    print("=" * 66)
    print("  漫流 MANFLOWS · 本机启停桥接服务 v%s" % VERSION)
    print("=" * 66)
    print("  监听地址： http://127.0.0.1:%d   （只监听本机）" % port)
    print("  配置文件： %s" % CONFIG_PATH)
    for key, svc in CONFIG["services"].items():
        print("  · %-8s 启动程序：%s" % (key, svc.get("path") or "（网页里填）"))
        print("  %-8s   健康检查：%s" % ("", svc.get("health")))
    print("  保持这个窗口开着，网页里的「启动 / 停止」才有效。")
    print("  关闭本窗口 = 停止桥接服务，已启动的 llama / ComfyUI 不受影响。")
    print("=" * 66)
    log("桥接服务启动，端口 %d" % port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("桥接服务退出")
        srv.server_close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--pick":
        pick_file_cli()
    else:
        main()
