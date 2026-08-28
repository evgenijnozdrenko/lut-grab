#!/usr/bin/env python3
"""LUT Grab — локальна качалка відео для ЛУТ.

Тонка обгортка над yt-dlp і ffmpeg. Слухає тільки 127.0.0.1, кожен запит
підписаний одноразовим токеном сесії. Нічого нікуди не відправляє.
"""
import json
import gzip
import io
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
import zipfile

try:
    # Рідне вікно на WKWebView. Опційне свідомо: у режимі розробки запускаємось
    # системним python3, де цієї бібліотеки немає, і тоді просто йдемо у браузер.
    import webview
except ImportError:
    webview = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Зібраний .app — це бандл тільки для читання: писати всередину не можна, інакше
# ламається підпис і система перестає його запускати. Тому в зібраному вигляді
# бінарники живуть в Application Support, а в режимі розробки — поруч зі скриптом.
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APPDIR = (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LUT Grab") if sys.platform == "win32" \
    else (Path.home() / "Library" / "Application Support" / "lut-grab")
BIN = (APPDIR / "bin") if FROZEN else Path(__file__).resolve().parent / "bin"
SESSION = APPDIR / "session.json"
OUT = Path.home() / "Downloads" / "LUT Grab"
PORT = int(os.environ.get("LUTGRAB_PORT", "8766"))
TOKEN = secrets.token_urlsafe(16)

IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""
YTDLP = "yt-dlp" + EXE
FFMPEG = "ffmpeg" + EXE
FFPROBE = "ffprobe" + EXE

GH = "https://github.com"
ARCH = "arm64" if platform.machine() == "arm64" else "x64"
FFMAC = f"{GH}/eugeneware/ffmpeg-static/releases/latest/download"
FFWIN = f"{GH}/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"


def install_plan(what):
    """Що саме качає кнопка. macOS дістає два окремих бінарники, Windows — один архів.

    Джерела віддають ad-hoc підписані збірки: без підпису macOS на Apple Silicon
    вбиває процес сигналом 9, і причина цього абсолютно неочевидна.
    """
    if what == "ytdlp":
        name = "yt-dlp.exe" if IS_WIN else "yt-dlp_macos"
        return [{"url": f"{GH}/yt-dlp/yt-dlp/releases/latest/download/{name}",
                 "kind": "raw", "name": YTDLP}]
    if what == "ffmpeg":
        if IS_WIN:
            return [{"url": FFWIN, "kind": "zip", "members": [FFMPEG, FFPROBE]}]
        return [{"url": f"{FFMAC}/ffmpeg-darwin-{ARCH}.gz", "kind": "gz", "name": FFMPEG},
                {"url": f"{FFMAC}/ffprobe-darwin-{ARCH}.gz", "kind": "gz", "name": FFPROBE}]
    return None
EXTRA_PATHS = [] if sys.platform == "win32" else ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]

TIMECODE = re.compile(r"^\d{1,3}(:[0-5]?\d){0,2}(\.\d{1,3})?$")
BROWSERS = {"safari", "chrome", "brave", "edge", "firefox", "opera", "vivaldi", "chromium"}

_current = {"proc": None}
_lock = threading.Lock()


# ─────────────────────────────  залежності  ─────────────────────────────

def _find(name, local=None):
    if local and local.exists() and os.access(local, os.X_OK):
        return str(local)
    hit = shutil.which(name)
    if hit:
        return hit
    for d in EXTRA_PATHS:
        p = Path(d) / name
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def find_ytdlp():
    return _find(YTDLP, BIN / YTDLP)


def find_ffmpeg():
    # Системний пріоритетніший за наш: його ставили свідомо, і він зазвичай свіжіший
    # за той, що лежить у релізах ffmpeg-static. Наш — запасний варіант.
    local = BIN / FFMPEG
    if hit := _find(FFMPEG):
        return hit
    return str(local) if local.exists() and os.access(local, os.X_OK) else None


def version_of(path):
    if not path:
        return None
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip().splitlines()[0][:60]
    except Exception:
        return None


# ─────────────────────────────  збірка команди  ─────────────────────────────

def clean_tc(v):
    v = (v or "").strip()
    if not v:
        return ""
    if not TIMECODE.match(v):
        raise ValueError(f"Таймкод «{v}» не читається. Формат: 90, 1:30 або 0:01:30")
    return v


def build_cmd(p, ytdlp, ffmpeg):
    url = (p.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Потрібне посилання, що починається з http:// або https://")

    a, b = clean_tc(p.get("start")), clean_tc(p.get("end"))
    # Відрізок іде в ім'я файлу. Інакше другий відрізок того самого відео
    # yt-dlp вважає вже завантаженим і мовчки нічого не робить.
    cut = f" [{(a or '0').replace(':', '-')}—{(b or 'кінець').replace(':', '-')}]" if (a or b) else ""

    cmd = [
        ytdlp, "--ignore-config", "--newline", "--no-colors", "--no-warnings",
        "--progress-template",
        "download:@P|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "-P", str(OUT),
        "-o", "%(title).100B [%(id)s]" + cut + ".%(ext)s",
        "--no-mtime", "--retries", "10", "--fragment-retries", "10",
        # Фінальні імена беремо в самого yt-dlp, а не вигрібаємо регексом з логу:
        # рядки «Merging formats into» і «Destination» описують ще й проміжні файли,
        # які потім видаляються, і звіт виходив порожнім або брехливим.
        "--print", "after_move:@F|%(filepath)s",
    ]
    if ffmpeg:
        cmd += ["--ffmpeg-location", str(Path(ffmpeg).parent)]

    cmd.append("--yes-playlist" if p.get("playlist") else "--no-playlist")

    br = (p.get("cookies") or "").strip().lower()
    if br:
        if br not in BROWSERS:
            raise ValueError(f"Невідомий браузер для cookies: {br}")
        cmd += ["--cookies-from-browser", br]

    if p.get("mode") == "thumb":
        # Обкладинка потрібна ютуберу окремо від відео: подивитись, як зроблено чуже
        # прев'ю. Тягнемо найбільшу доступну і конвертуємо у jpg, бо YouTube віддає webp,
        # який не відкривається половиною редакторів.
        cmd += ["--skip-download", "--write-thumbnail"]
        if ffmpeg:
            cmd += ["--convert-thumbnails", "jpg"]
        cmd += ["--", url]
        return cmd

    if p.get("mode") == "audio":
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
        if ffmpeg:
            cmd += ["--embed-thumbnail", "--embed-metadata"]
    else:
        h = int(p.get("height") or 1080)
        h = max(144, min(h, 4320))
        if p.get("apple", True):
            # H.264 + AAC — єдина пара, яку QuickTime, Фото і Final Cut відкривають
            # без бубна. YouTube роздає H.264 максимум у 1080p, вище — тільки VP9 і AV1,
            # тому ця галочка свідомо опускає якість замість того, щоб віддати файл,
            # який не програється. Саме на AV1 і зламалось перше завантаження.
            fmt = (f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/"
                   f"b[height<={h}][vcodec^=avc1]/"
                   f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]")
        else:
            fmt = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        cmd += ["-f", fmt, "--merge-output-format", "mp4"]
        if ffmpeg:
            cmd += ["--embed-metadata"]

    if a or b:
        if not ffmpeg:
            raise ValueError("Різати відрізки без ffmpeg неможливо. Постав ffmpeg.")
        cmd += ["--download-sections", f"*{a or '0'}-{b or 'inf'}"]
        if p.get("exact", True):
            cmd.append("--force-keyframes-at-cuts")

    if p.get("subs"):
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "uk.*,en.*",
                "--convert-subs", "srt"]

    cmd += ["--", url]
    return cmd


class _Meter:
    """Лічильник прочитаних байтів поверх потоку відповіді."""

    def __init__(self, fp):
        self.fp, self.n = fp, 0

    def read(self, size=-1):
        b = self.fp.read(size)
        self.n += len(b)
        return b


# ─────────────────────────────  HTTP  ─────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "LUT Grab"

    def log_message(self, *a):
        pass

    # -- helpers -------------------------------------------------------

    def guard(self, q):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            self.send_error(403, "bad host")
            return False
        if q.get("t", [""])[0] != TOKEN:
            self.send_error(403, "bad token")
            return False
        return True

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def open_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def sse(self, event, data):
        try:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ValueError):
            return False

    # -- routes --------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        if route == "/":
            return self.serve_ui(q)
        if not self.guard(q):
            return

        if route == "/api/check":
            y, f = find_ytdlp(), find_ffmpeg()
            return self.send_json({
                "ytdlp": y, "ffmpeg": f,
                "ytdlpVersion": version_of(y),
                "ffmpegVersion": version_of(f),
                "out": str(OUT),
            })
        if route == "/api/install":
            return self.install(q.get("what", ["ytdlp"])[0])
        if route == "/api/info":
            return self.info(q)
        if route == "/api/download":
            return self.download(q)
        if route == "/api/cancel":
            with _lock:
                proc = _current["proc"]
            if proc and proc.poll() is None:
                try:
                    if IS_WIN:
                        # yt-dlp плодить дочірні ffmpeg — валимо все дерево.
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                       capture_output=True)
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    proc.terminate()
            return self.send_json({"ok": True})
        if route == "/api/quit":
            self.send_json({"ok": True})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        if route == "/api/tg":
            # Посилання відкриваємо системним браузером, а не всередині вікна:
            # інакше телеграм відкрився б у вікні качалки і звідти нікуди не дітись.
            link = "https://t.me/lootaimo"
            if IS_WIN:
                os.startfile(link)
            else:
                subprocess.run(["open", link])
            return self.send_json({"ok": True})
        if route == "/api/reveal":
            OUT.mkdir(parents=True, exist_ok=True)
            subprocess.run(["explorer", str(OUT)] if IS_WIN else ["open", str(OUT)])
            return self.send_json({"ok": True})
        self.send_error(404)

    def serve_ui(self, q):
        if q.get("t", [""])[0] != TOKEN:
            body = ("<meta charset=utf-8><body style='font:16px system-ui;padding:40px'>"
                    "Ця вкладка застаріла. Перезапусти LUT Grab.</body>").encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        html = (ROOT / "ui.html").read_bytes().replace(b"__TOKEN__", TOKEN.encode())
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html)

    def install(self, what):
        self.open_sse()
        plan = install_plan(what)
        if not plan:
            return self.sse("done", {"ok": False, "error": "невідомий компонент"})
        BIN.mkdir(parents=True, exist_ok=True)
        try:
            for i, step in enumerate(plan, 1):
                label = step.get("name") or "ffmpeg"
                self.sse("log", f"Качаю {label} ({i} з {len(plan)})…")
                if step["kind"] == "zip":
                    self.fetch_zip(step, i, len(plan))
                else:
                    self.fetch_binary(step, i, len(plan))
            want = [step.get("name") for step in plan if step.get("name")] or [FFMPEG, FFPROBE]
            missing = [n for n in want if not version_of(str(BIN / n))]
            if missing:
                raise RuntimeError(f"{', '.join(missing)} завантажився, але не запускається")
            self.sse("done", {"ok": True})
        except Exception as e:
            self.sse("done", {"ok": False, "error": f"{e}"})

    def _open(self, url, on_progress):
        req = urllib.request.Request(url, headers={"User-Agent": "LUT Grab"})
        return urllib.request.urlopen(req, timeout=300)

    def fetch_binary(self, step, idx, total):
        name = step["name"]
        dest, tmp = BIN / name, BIN / (name + ".part")
        with self._open(step["url"], None) as r:
            size = int(r.headers.get("Content-Length") or 0)
            # Прогрес рахуємо по стиснених байтах з мережі — тільки вони співвідносяться
            # з Content-Length. Взяти їх у HTTPResponse через tell() не можна: він кидає
            # UnsupportedOperation, бо потік не перемотується. Тому рахуємо самі.
            meter = _Meter(r)
            raw = gzip.GzipFile(fileobj=meter) if step["kind"] == "gz" else meter
            with open(tmp, "wb") as f:
                while True:
                    chunk = raw.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    if size:
                        base = (idx - 1) * 100 / total
                        self.sse("progress", {"percent": round(base + min(meter.n / size, 1) * 100 / total, 1)})
        self.finalize(tmp, dest)

    def fetch_zip(self, step, idx, total):
        """Windows-збірка ffmpeg приїжджає одним архівом на обидва бінарники."""
        buf = io.BytesIO()
        with self._open(step["url"], None) as r:
            size = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                buf.write(chunk)
                got += len(chunk)
                if size:
                    base = (idx - 1) * 100 / total
                    self.sse("progress", {"percent": round(base + min(got / size, 1) * 100 / total, 1)})
        with zipfile.ZipFile(buf) as z:
            for want in step["members"]:
                # Усередині архіву шлях містить версію, тому шукаємо по хвосту.
                hit = next((n for n in z.namelist() if n.endswith("bin/" + want)), None)
                if not hit:
                    raise RuntimeError(f"в архіві немає {want}")
                tmp = BIN / (want + ".part")
                with z.open(hit) as src, open(tmp, "wb") as f:
                    shutil.copyfileobj(src, f)
                self.finalize(tmp, BIN / want)

    @staticmethod
    def finalize(tmp, dest):
        tmp.replace(dest)
        dest.chmod(0o755)
        if sys.platform == "darwin":
            # Знімаємо карантин: інакше Gatekeeper мовчки вбиває щойно завантажений бінарник.
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], capture_output=True)

    def info(self, q):
        ytdlp = find_ytdlp()
        if not ytdlp:
            return self.send_json({"error": "yt-dlp не знайдено"}, 400)
        url = q.get("url", [""])[0].strip()
        if not url.lower().startswith(("http://", "https://")):
            return self.send_json({"error": "Потрібне повне посилання http(s)://"}, 400)
        cmd = [ytdlp, "--ignore-config", "-J", "--no-warnings", "--flat-playlist", "--", url]
        br = q.get("cookies", [""])[0].strip().lower()
        if br in BROWSERS:
            cmd[2:2] = ["--cookies-from-browser", br]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return self.send_json({"error": "yt-dlp не відповів за 90 секунд"}, 504)
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()
            return self.send_json({"error": tail[-1] if tail else "yt-dlp повернув помилку"}, 400)
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            return self.send_json({"error": "не вдалося прочитати відповідь yt-dlp"}, 500)

        entries = d.get("entries") or []
        formats = d.get("formats") or []
        heights = sorted({f.get("height") for f in formats
                          if isinstance(f.get("height"), int)}, reverse=True)
        # Окремо — висоти, де є H.264: тільки вони гарантовано відкриваються на Mac.
        avc = sorted({f.get("height") for f in formats
                      if isinstance(f.get("height"), int)
                      and str(f.get("vcodec") or "").startswith("avc1")}, reverse=True)
        return self.send_json({
            "title": d.get("title") or "без назви",
            "uploader": d.get("uploader") or d.get("channel") or "",
            "duration": d.get("duration"),
            "thumbnail": d.get("thumbnail"),
            "isPlaylist": d.get("_type") == "playlist",
            "count": len(entries),
            "heights": heights,
            "avcHeights": avc,
        })

    def download(self, q):
        ytdlp, ffmpeg = find_ytdlp(), find_ffmpeg()
        self.open_sse()
        if not ytdlp:
            return self.sse("done", {"ok": False, "error": "yt-dlp не знайдено"})
        with _lock:
            busy = _current["proc"] and _current["proc"].poll() is None
        if busy:
            return self.sse("done", {"ok": False, "error": "Одне завантаження вже йде"})
        try:
            payload = json.loads(q.get("p", ["{}"])[0])
            cmd = build_cmd(payload, ytdlp, ffmpeg)
        except (ValueError, json.JSONDecodeError) as e:
            return self.sse("done", {"ok": False, "error": str(e)})

        OUT.mkdir(parents=True, exist_ok=True)
        # Знімок теки до старту: для обкладинок yt-dlp не викликає after_move,
        # тому імена файлів доводиться діставати різницею.
        before = {f.name for f in OUT.iterdir()}
        self.sse("log", "Запускаю yt-dlp…")
        # Своя група процесів, щоб «Зупинити» валило і дочірній ffmpeg разом з yt-dlp.
        # На Windows замість цього ховаємо консольне вікно, яке інакше блимає на весь екран.
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {"start_new_session": True}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, encoding="utf-8", errors="replace", **extra)
        with _lock:
            _current["proc"] = proc
        files = []
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("@P|"):
                    _, pct, speed, eta = line.split("|", 3)
                    self.sse("progress", {"percent": pct.strip().rstrip("%"),
                                          "speed": speed.strip(), "eta": eta.strip()})
                    continue
                if line.startswith("@F|"):
                    files.append(Path(line[3:].strip()).name)
                    continue
                if not self.sse("log", line):
                    break
        finally:
            code = proc.wait()
            with _lock:
                _current["proc"] = None
        # yt-dlp рапортує і проміжні файли (webm до конвертації в mp3), які потім видаляє —
        # тому лишаємо тільки те, що реально долетіло на диск.
        seen, uniq = set(), []
        for f in files:
            if f not in seen and (OUT / f).exists():
                seen.add(f)
                uniq.append(f)
        if not uniq:
            uniq = sorted(f.name for f in OUT.iterdir() if f.name not in before)
        self.sse("done", {"ok": code == 0, "code": code, "files": uniq, "out": str(OUT)})


def already_running():
    """Друга копія не падає з «порт зайнятий» — вона просто відкриває вкладку першої."""
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.4)
        if sock.connect_ex(("127.0.0.1", PORT)) != 0:
            return False
    try:
        old = json.loads(SESSION.read_text())
        webbrowser.open(f"http://127.0.0.1:{old['port']}/?t={old['token']}")
    except Exception:
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
    return True


def main():
    if already_running():
        return
    OUT.mkdir(parents=True, exist_ok=True)
    APPDIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/?t={TOKEN}"
    SESSION.write_text(json.dumps({"port": PORT, "token": TOKEN, "pid": os.getpid()}))
    SESSION.chmod(0o600)
    print(f"\n  LUT Grab запущено\n  {url}\n  тека: {OUT}\n")

    if webview is not None:
        # GUI на macOS живе тільки в головному потоці, тому сервер іде у фоновий.
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            webview.create_window("LUT Grab", url, width=940, height=1080,
                                  min_size=(620, 700))
            webview.start()
            os._exit(0)  # вікно закрили - програма скінчилась, разом із сервером
        except Exception:
            # У зібраному .app stdout іде в нікуди, тому причину пишемо у файл,
            # інакше збій вікна виглядає як «програма просто не запустилась».
            import traceback
            (APPDIR / "last-error.log").write_text(traceback.format_exc())
            print("  Вікно не піднялось, відкриваю браузер")

    print("  Ctrl+C щоб зупинити\n")
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Зупинено.\n")
        srv.shutdown()


if __name__ == "__main__":
    main()
