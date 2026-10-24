"""
Screen Assistant (Gemini free-tier version)
-------------------------------------------
F8  = analyze the screen (short answer / insight)
F9  = ask your own question about the screen
F10 = dismiss the box + quiz X (also cancels a request in progress)
F12 = quit the app (it has no dock/taskbar icon)

Get a free key at https://aistudio.google.com/apikey  (no billing needed).
It's asked once on first launch and saved in ~/.screen_assistant.json
(or set the GEMINI_API_KEY environment variable, or fill EMBEDDED_KEY below).
Crashes/errors are logged to ~/.screen_assistant.log
"""

import base64
import io
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import simpledialog, messagebox

if sys.platform == "win32":                     # crisp text, correct sizes on high-DPI
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import mss
import requests
from PIL import Image
from pynput import keyboard

# ───────────────────────────── CONFIG ─────────────────────────────
# "-latest" aliases follow Google's current model so this keeps working as they
# retire old ones. If it ever fails, the app auto-picks an available Flash model.
MODEL = "gemini-flash-lite-latest"        # fastest. Try "gemini-flash-latest" for smarter answers
HOTKEY_ANALYZE = "<f8>"
HOTKEY_ASK = "<f9>"
HOTKEY_DISMISS = "<f10>"
HOTKEY_QUIT = "<f12>"

BOX_WIDTH = 460
TOP_OFFSET = 90                   # keeps the box below browser tabs
OPACITY = 0.94
HIDE_FROM_SCREEN_SHARE = True     # box is invisible to screen sharing / recording
FONT_FAMILY = {"win32": "Segoe UI", "darwin": "Helvetica Neue"}.get(sys.platform, "DejaVu Sans")
FONT = (FONT_FAMILY, 11)
BG, FG = "#1e1e24", "#eaeaf0"

MAX_IMAGE_WIDTH = 1280
JPEG_QUALITY = 70
MAX_TOKENS = 260
MAX_CHARS = 380
AUTO_HIDE_BASE_SEC = 6
AUTO_HIDE_PER_CHAR = 0.05
# Quiz marker: a small black X drawn on screen next to the correct answer
QUIZ_MARKER = True
QUIZ_X_POSITION = "right"        # "right" = just right of the answer text, "on" = centered on it
QUIZ_X_SIZE = 16                 # pixels
QUIZ_X_SECONDS = 25              # how long the X stays (F10 / new request clears it sooner)
QUIZ_TEXT_BOX = True             # False = quizzes show ONLY the X, no text box
MIN_SECONDS_BETWEEN_REQUESTS = 0.7   # stops accidental key-repeat from burning your free quota

SYSTEM_PROMPT = (
    "You are a quiet screen assistant. The user sends a screenshot. "
    "STRICT FORMAT: maximum 3 lines. Each line under 15 words. Start each line with '• '. "
    "Never write paragraphs. No preamble, no 'The screenshot shows', no closing remarks. "
    "If there is a visible question, quiz, error or problem, put the direct answer on line 1. "
    "Otherwise give the 2-3 most useful points about what's on screen. "
    "If the user asks something very simple, a single line is fine. "
    "IGNORE RULE: completely ignore people's faces, webcam or video-call tiles, profile photos, "
    "ads, banners, menus, sidebars, notifications, taskbars, wallpapers and any decorative or unrelated "
    "clutter. Never describe, mention or identify any person or their appearance. Focus only on the main "
    "content the user is working on (the question, text, code, error, document or quiz)."
    + (
    " QUIZ RULE: if the screen shows a quiz, multiple-choice, or true/false question, "
    "line 1 is the correct answer (like '• B) Paris'). Then, after the bullets, for EACH correct "
    "option that is visible on screen, output on its own line exactly: @@BOX ymin,xmin,ymax,xmax@@ "
    "= the tight bounding box of that option's text (including its radio button/checkbox), as integers "
    "0-1000 normalized to the screenshot (y first, then x; 0,0 is the top-left corner). "
    "Only use @@BOX for quizzes where the correct option is visible. Never use it otherwise."
    if QUIZ_MARKER else "")
)
DEFAULT_QUESTION = "Direct answer or the key points about the main content on my screen. Ignore faces and clutter."
CONFIG_FILE = Path.home() / ".screen_assistant.json"
LOG_FILE = Path.home() / ".screen_assistant.log"
# For a no-setup build for one trusted person, put a FREE Gemini key here (or let the
# GitHub workflow fill it in from a secret). Leave "" to make the app ask for a key instead.
EMBEDDED_KEY = ""
API_BASE = os.environ.get("SCREEN_ASSISTANT_API_BASE",
                          "https://generativelanguage.googleapis.com/v1beta")
# ───────────────────────────────────────────────────────────────────

API_KEY = ""
current_model = MODEL
http = requests.Session()                        # keeps the connection open between requests
events: "queue.Queue[tuple]" = queue.Queue()
_job_lock = threading.Lock()
_latest_job = 0
_last_start = 0.0
_instance_lock = None


# ─────────────────────────── logging / safety ───────────────────────────
def log(msg: str):
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 200_000:
            LOG_FILE.write_text("")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def install_crash_logging():
    def hook(exc_type, exc, tb):
        log("UNCAUGHT: " + "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def safe(fn):
    """Wrap a hotkey callback: an exception there would silently kill the hotkey listener."""
    def wrapper():
        try:
            fn()
        except Exception:
            log("HOTKEY ERROR: " + traceback.format_exc())
    return wrapper


def single_instance() -> bool:
    """Only one copy at a time (double-clicking twice would duplicate every hotkey)."""
    global _instance_lock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 48653))
        s.listen(1)
    except OSError:
        return False
    _instance_lock = s
    return True


# ─────────────────────────── job bookkeeping ───────────────────────────
def new_job() -> int:
    global _latest_job
    with _job_lock:
        _latest_job += 1
        return _latest_job


def cancel_jobs():
    """Invalidate anything in flight (used by dismiss) so late results never reappear."""
    new_job()


def is_current(job: int) -> bool:
    return job == _latest_job


# ─────────────────────────── helpers ───────────────────────────
class AIError(Exception):
    pass


def get_api_key() -> str:
    if EMBEDDED_KEY:
        return EMBEDDED_KEY
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        key = json.loads(CONFIG_FILE.read_text()).get("gemini_api_key", "").strip()
        if key:
            return key
    except Exception:
        pass
    key = (simpledialog.askstring(
        "Screen Assistant setup",
        "Paste your free Gemini API key\n(get one at aistudio.google.com/apikey):",
        show="*") or "").strip()
    if key:
        try:
            CONFIG_FILE.write_text(json.dumps({"gemini_api_key": key}))
            CONFIG_FILE.chmod(0o600)
        except Exception:
            log("Could not save key file")
    return key


BOX_RE = re.compile(
    r"@@\s*BOX\s*\[?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*"
    r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]?\s*(?:@@|$)", re.M)


def parse_boxes(text: str):
    """Return [(ymin, xmin, ymax, xmax), ...] as 0..1 fractions of the screen."""
    boxes = []
    for m in BOX_RE.finditer(text):
        y0, x0, y1, x1 = (max(0.0, min(1000.0, float(v))) / 1000.0 for v in m.groups())
        if y1 < y0:
            y0, y1 = y1, y0
        if x1 < x0:
            x0, x1 = x1, x0
        boxes.append((y0, x0, y1, x1))
    return boxes[:6]


def tidy(text: str) -> str:
    text = text.split("@@")[0].rstrip("@")            # hide the machine-readable marker lines
    text = re.sub(r"\n{2,}", "\n", text.strip())
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        best = max(cut.rfind(c) for c in (".", "!", "?", "\n"))
        text = cut[:best + 1] if best > MAX_CHARS * 0.4 else cut.rsplit(" ", 1)[0] + "…"
    return text


def capture_screen_b64() -> str:
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if img.width > MAX_IMAGE_WIDTH:
            ratio = MAX_IMAGE_WIDTH / img.width
            img = img.resize((MAX_IMAGE_WIDTH, max(1, int(img.height * ratio))), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log("CAPTURE ERROR: " + traceback.format_exc())
        hint = " On Mac: allow Screen Recording in System Settings." if sys.platform == "darwin" else ""
        raise AIError("Couldn't capture the screen." + hint) from e


# ─────────────────────────── Gemini ───────────────────────────
def pick_available_model() -> str:
    r = http.get(f"{API_BASE}/models", params={"pageSize": 200},
                 headers={"x-goog-api-key": API_KEY}, timeout=15)
    r.raise_for_status()
    names = []
    for m in r.json().get("models", []):
        n = m.get("name", "").removeprefix("models/")
        if ("generateContent" in m.get("supportedGenerationMethods", [])
                and "flash" in n
                and not any(bad in n for bad in ("image", "tts", "live", "audio", "embed", "robotics", "computer"))):
            names.append(n)
    if not names:
        raise AIError("No Gemini Flash model available for this key.")
    lite = sorted([n for n in names if "lite" in n], reverse=True)
    return (lite or sorted(names, reverse=True))[0]


def _stream_request(model: str, body: dict):
    return http.post(
        f"{API_BASE}/models/{model}:streamGenerateContent",
        params={"alt": "sse"},
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json=body, stream=True, timeout=(5, 20),
    )


def open_stream(body: dict):
    """POST with automatic fallbacks: drop unsupported thinking option, re-pick a retired model, retry once on 5xx."""
    global current_model
    dropped_thinking = picked = retried = False
    while True:
        r = _stream_request(current_model, body)
        sc = r.status_code
        if sc == 400 and not dropped_thinking and "thinking" in r.text.lower():
            dropped_thinking = True
            body["generationConfig"].pop("thinkingConfig", None)
            body["generationConfig"]["maxOutputTokens"] = MAX_TOKENS * 3
            continue
        if sc == 404 and not picked:
            picked = True
            current_model = pick_available_model()
            continue
        if sc in (500, 502, 503, 504) and not retried:
            retried = True
            r.close()
            time.sleep(0.5)
            continue
        return r


def ask_ai_streaming(question: str, image_b64: str, job: int):
    """Returns (text, boxes), or None if this job was dismissed/superseded."""
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
            {"text": question},
        ]}],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS,
            "temperature": 0.3,
            "thinkingConfig": {"thinkingBudget": 0},     # no hidden "thinking" = faster
        },
    }
    r = open_stream(body)
    try:
        if r.status_code == 429:
            raise AIError("Free limit reached. Wait a minute (or check aistudio.google.com/rate-limit).")
        if r.status_code in (401, 403) or (r.status_code == 400 and "api key" in r.text.lower()):
            raise AIError("API key rejected. Delete ~/.screen_assistant.json and restart.")
        if r.status_code != 200:
            raise AIError(f"Gemini error {r.status_code}: {r.text[:100]}")

        acc, last_push = "", 0.0
        for raw in r.iter_lines():
            if not is_current(job):
                return None                              # dismissed or replaced: stop immediately
            if not raw or not raw.startswith(b"data:"):
                continue
            try:
                data = json.loads(raw[5:].strip())
            except ValueError:
                continue
            block = (data.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise AIError("Gemini declined to read this screen.")
            for cand in data.get("candidates", []):
                for part in (cand.get("content") or {}).get("parts", []):
                    if part.get("text") and not part.get("thought"):
                        acc += part["text"]
            now = time.monotonic()
            if now - last_push > 0.06:                   # throttle UI updates = no flicker
                events.put(("live", job, acc))
                last_push = now
        return tidy(acc), (parse_boxes(acc) if QUIZ_MARKER else [])
    finally:
        r.close()


def worker(question: str, job: int):
    try:
        events.put(("hide",))
        time.sleep(0.06)                                 # let our own box vanish before the screenshot
        if not is_current(job):
            return
        img = capture_screen_b64()
        events.put(("status", job, "…"))
        result = ask_ai_streaming(question, img, job)
        if result is not None:
            text, boxes = result
            events.put(("done", job, text or "Nothing useful to say about this.", boxes))
    except AIError as e:
        events.put(("error", job, str(e)))
    except requests.exceptions.RequestException:
        events.put(("error", job, "Network problem. Check your connection."))
    except Exception:  # noqa: BLE001
        log("WORKER ERROR: " + traceback.format_exc())
        events.put(("error", job, "Something went wrong (see ~/.screen_assistant.log)."))


def start_request(question: str):
    """Begin a request; a newer one replaces any older one. Ignores key-repeat spam."""
    global _last_start
    now = time.monotonic()
    if now - _last_start < MIN_SECONDS_BETWEEN_REQUESTS:
        return
    _last_start = now
    job = new_job()
    threading.Thread(target=worker, args=(question, job), daemon=True).start()


def warm_up():
    """Open the connection ahead of time so the first real request is fast."""
    try:
        http.get(f"{API_BASE}/models", params={"pageSize": 1},
                 headers={"x-goog-api-key": API_KEY}, timeout=10)
    except Exception:
        pass


# ─────────────────────────── overlay window ───────────────────────────
def hide_from_capture(win: tk.Toplevel):
    """Ask the OS to leave this window out of screen capture (it stays visible to you)."""
    if not HIDE_FROM_SCREEN_SHARE:
        return
    try:
        win.update_idletasks()
        if sys.platform == "win32":
            import ctypes
            u = ctypes.windll.user32
            u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            u.GetAncestor.restype = ctypes.c_void_p
            u.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            hwnd = u.GetAncestor(win.winfo_id(), 2)            # GA_ROOT
            u.SetWindowDisplayAffinity(hwnd, 0x11)             # WDA_EXCLUDEFROMCAPTURE
        elif sys.platform == "darwin":
            from AppKit import NSApp
            for w in NSApp.windows():
                w.setSharingType_(0)                           # NSWindowSharingNone
    except Exception:
        pass


def make_click_through(win: tk.Toplevel, title: str):
    """Let mouse clicks pass through this window, and keep it out of Alt-Tab / taskbar."""
    try:
        win.update_idletasks()
        if sys.platform == "win32":
            import ctypes
            u = ctypes.windll.user32
            u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            u.GetAncestor.restype = ctypes.c_void_p
            u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            u.GetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            hwnd = u.GetAncestor(win.winfo_id(), 2)
            style = u.GetWindowLongW(hwnd, -20)                # GWL_EXSTYLE
            u.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80)   # LAYERED | TRANSPARENT | TOOLWINDOW
        elif sys.platform == "darwin":
            from AppKit import NSApp
            for w in NSApp.windows():
                if str(w.title()) == title:
                    w.setIgnoresMouseEvents_(True)
    except Exception:
        pass


class Marker:
    """Small black X windows drawn next to the correct quiz answer(s)."""
    KEY = "#010203"                                   # colour that becomes see-through on Windows

    def __init__(self, root: tk.Tk):
        self.root = root
        self.wins = []
        self.job = None

    def clear(self):
        if self.job:
            try:
                self.root.after_cancel(self.job)
            except Exception:
                pass
            self.job = None
        for w in self.wins:
            try:
                w.destroy()
            except Exception:
                pass
        self.wins = []

    def show(self, boxes):
        self.clear()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        size = QUIZ_X_SIZE
        for (y0, x0, y1, x1) in boxes:
            cy = (y0 + y1) / 2 * sh
            cx = ((x0 + x1) / 2 * sw) if QUIZ_X_POSITION == "on" else (x1 * sw + size / 2 + 6)
            left = int(max(0, min(sw - size, cx - size / 2)))
            top = int(max(0, min(sh - size, cy - size / 2)))
            try:
                self.wins.append(self._make(left, top, size))
            except Exception:
                log("MARKER ERROR: " + traceback.format_exc())
        if self.wins:
            self.job = self.root.after(int(QUIZ_X_SECONDS * 1000), self.clear)

    def _make(self, left: int, top: int, size: int) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.title("sa_marker")
        win.attributes("-topmost", True)
        bg = "white"
        if sys.platform == "win32":
            bg = self.KEY
            win.attributes("-transparentcolor", self.KEY)
        elif sys.platform == "darwin":
            bg = "systemTransparent"
            win.attributes("-transparent", True)
        win.configure(bg=bg)
        c = tk.Canvas(win, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        c.pack()
        pad = 3
        c.create_line(pad, pad, size - pad, size - pad, width=3, fill="#000000", capstyle="round")
        c.create_line(size - pad, pad, pad, size - pad, width=3, fill="#000000", capstyle="round")
        win.geometry(f"{size}x{size}+{left}+{top}")
        hide_from_capture(win)
        make_click_through(win, "sa_marker")
        return win


class Overlay:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.marker = Marker(root)
        self.win = None
        self.label = None
        self.hide_job = None
        self._geom = None

    def _make_window(self) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-alpha", OPACITY)
        except tk.TclError:
            pass
        win.configure(bg="#3a3a46")
        self._geom = None
        return win

    def _place(self, win: tk.Toplevel):
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = max(0, (sw - w) // 2)
        y = min(TOP_OFFSET, max(0, sh - h - 20))
        geom = f"{w}x{h}+{x}+{y}"
        if geom != self._geom:                       # only touch the window when its size changed
            win.geometry(geom)
            self._geom = geom

    def show(self, text: str, seconds: float | None = None,
             reuse: bool = False, autohide: bool = True):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        if reuse and self.win is not None and self.label is not None:
            self.label.config(text=text)           # update in place: no flicker while streaming
            self._place(self.win)
        else:
            self.hide()
            win = self.win = self._make_window()
            max_w = min(BOX_WIDTH, win.winfo_screenwidth() - 40)
            self.label = tk.Label(
                win, text=text, font=FONT, bg=BG, fg=FG, justify="left", anchor="w",
                wraplength=max_w - 32, padx=14, pady=10, cursor="hand2",
            )
            self.label.pack(padx=1, pady=1)
            self.label.bind("<Button-1>", lambda _e: events.put(("dismiss",)))
            self._place(win)
            hide_from_capture(win)
        if autohide:
            if seconds is None:
                seconds = AUTO_HIDE_BASE_SEC + len(text) * AUTO_HIDE_PER_CHAR
            self.hide_job = self.root.after(int(seconds * 1000), self.hide)

    def hide(self):
        if self.hide_job:
            try:
                self.root.after_cancel(self.hide_job)
            except Exception:
                pass
            self.hide_job = None
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self.label = None

    def hide_all(self):
        self.hide()
        self.marker.clear()

    def prompt(self):
        self.hide_all()
        win = self.win = self._make_window()
        entry = tk.Entry(win, font=FONT, bg=BG, fg=FG, insertbackground=FG,
                         relief="flat", width=44)
        entry.pack(padx=1, pady=1, ipady=8, ipadx=6)

        def submit(_e=None):
            q = entry.get().strip()
            self.hide()
            if q:
                start_request(q)

        entry.bind("<Return>", submit)
        entry.bind("<Escape>", lambda _e: self.hide())
        self._place(win)
        hide_from_capture(win)
        try:
            win.focus_force()
            entry.focus_force()
        except Exception:
            pass


def make_poller(root: tk.Tk, overlay: Overlay):
    """Runs on the UI thread every 20ms; the ONLY place that touches the windows."""
    state = {"running": True}

    def handle(ev, live_only_last):
        kind = ev[0]
        if kind == "live":
            live_only_last["ev"] = ev                 # keep only the newest streamed text
            return
        live_only_last["ev"] = None                   # anything newer supersedes pending live text
        if kind == "status" and is_current(ev[1]):
            overlay.show(ev[2], 30, reuse=True)
        elif kind == "done" and is_current(ev[1]):
            boxes = ev[3] if len(ev) > 3 else []
            if boxes and not QUIZ_TEXT_BOX:
                overlay.hide()
            else:
                overlay.show(ev[2], reuse=True)
            if boxes:
                overlay.marker.show(boxes)
        elif kind == "error" and is_current(ev[1]):
            overlay.show(ev[2], 8, reuse=True)
        elif kind == "notice":
            overlay.show(ev[1], ev[2])
        elif kind == "hide":
            overlay.hide_all()
        elif kind == "dismiss":
            cancel_jobs()
            overlay.hide_all()
        elif kind == "prompt":
            overlay.prompt()
        elif kind == "quit":
            state["running"] = False
            root.quit()

    def poll():
        pending = {"ev": None}
        try:
            while True:
                try:
                    ev = events.get_nowait()
                except queue.Empty:
                    break
                try:
                    handle(ev, pending)
                except Exception:
                    log("UI ERROR: " + traceback.format_exc())
            live = pending["ev"]
            if live and is_current(live[1]):
                overlay.show(tidy(live[2]), reuse=True, autohide=False)
        except Exception:
            log("POLL ERROR: " + traceback.format_exc())
        finally:
            if state["running"]:
                root.after(20, poll)

    return poll


def main():
    global API_KEY
    install_crash_logging()
    if not single_instance():
        return                                        # already running
    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda *a: log("TK ERROR: " + "".join(traceback.format_exception(*a)))

    API_KEY = get_api_key()
    if not API_KEY:
        messagebox.showerror("Screen Assistant", "No API key provided. Exiting.")
        return

    overlay = Overlay(root)
    hotkeys = keyboard.GlobalHotKeys({
        HOTKEY_ANALYZE: safe(lambda: start_request(DEFAULT_QUESTION)),
        HOTKEY_ASK: safe(lambda: events.put(("prompt",))),
        HOTKEY_DISMISS: safe(lambda: (cancel_jobs(), events.put(("hide",)))),
        HOTKEY_QUIT: safe(lambda: events.put(("quit",))),
    })
    hotkeys.daemon = True
    hotkeys.start()                                   # hotkeys are live immediately
    threading.Thread(target=warm_up, daemon=True).start()

    events.put(("notice", "Screen Assistant ready\n• F8 analyze  • F9 ask  • F10 dismiss  • F12 quit", 5))
    make_poller(root, overlay)()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    except Exception:
        log("MAINLOOP ERROR: " + traceback.format_exc())
    finally:
        try:
            hotkeys.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
