#!/usr/bin/env python3
"""Morrowind sidecar: owns OpenMW, serves screenshots and a web UI.

The model plays from screenshots only. The model-facing endpoints (/act, /look,
/reset, /screen.jpg) return the step number, how much the screen changed,
argument errors and the image, never game state. The mod's state snapshot is
kept for run logs, milestones and the human web UI (/status, /log).

Endpoints (port 8342 by default, bound to 127.0.0.1):
  GET  /              web UI (live screen, step table, debug state)
  GET  /screen.jpg    latest screenshot sent to the model
  GET  /status        json: run, step, last action, milestones, debug state (human only)
  GET  /log           json: recorded actions for the current run (human only)
  POST /act           {"action": ..., ...}: run one action, take a screenshot,
                      return {step, changed}
  POST /look          fresh screenshot without acting
  POST /reset         start a run; ?name=RUN-ID, ?fresh=1 restarts the game
  POST /release       unpause the game and hand it back to a human
  POST /reload-lua    dev: reload the mod's Lua scripts in the running game

How it talks to the game: the agent mod (mod/scripts/piagent) polls
mod/piagent/cmd.txt through OpenMW's VFS for a command written as a Lua table
literal, executes it and prints `PIAGENT {json}` to
openmw.log. The world stays live between actions unless PI_PAUSE=1.
Menus (dialogue, name entry, race selection) are driven with
synthesized macOS mouse and keyboard events instead.

Needs Screen Recording + Accessibility permission for the app that runs it.

Configuration via environment:
  OPENMW_BIN   OpenMW binary (default /Applications/OpenMW.app/Contents/MacOS/openmw)
  GAME_PORT    HTTP port (default 8342)
  MODEL_WIDTH  width of screenshots sent to the model (default 1024)
  PI_REFOCUS   after a click, give focus back to the previous app (default 1)
  PI_PAUSE     1 = freeze the world between actions while the model thinks
               (default 0: the world stays live)
"""
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import AppKit
import ApplicationServices as AS
import Quartz
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent
MOD = ROOT / "mod"
CMD_FILE = MOD / "piagent" / "cmd.txt"
RUNS = ROOT / "runs"
CFG_DIR = RUNS / "openmw-config"
USER_DATA = RUNS / "userdata"
GAME_LOG = CFG_DIR / "openmw.log"
OPENMW_BIN = os.environ.get("OPENMW_BIN", "/Applications/OpenMW.app/Contents/MacOS/openmw")
PORT = int(os.environ.get("GAME_PORT", "8342"))
MODEL_WIDTH = int(os.environ.get("MODEL_WIDTH", "1024"))
REFOCUS = os.environ.get("PI_REFOCUS", "1") != "0"
PAUSE_BETWEEN = os.environ.get("PI_PAUSE", "0") == "1"

LUA_TIMEOUT = 60.0
BOOT_TIMEOUT = 120.0

# macOS virtual key codes (US layout positions; SDL maps them to scancodes,
# so movement keys work whatever the active keyboard layout is).
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "`": 50, "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51,
    "escape": 53, "esc": 53, "delete": 117, "home": 115, "end": 119, "pageup": 116,
    "pagedown": 121, "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

WORLD_ACTIONS = {"move", "turn", "activate", "jump", "wait", "stance", "attack"}
UI_ACTIONS = {"click", "type", "key"}
FORBIDDEN_KEYS = {"`"}  # the console would give the model a way around the screen-only rule

_lock = threading.Lock()          # one action at a time
_results = {}                     # seq -> result json from the mod
_results_cv = threading.Condition()
_seq = 0
_nonce = secrets.token_hex(4)
_game = None                      # Popen when we launched the game
_game_pid = None
_window = None                    # cached CGWindowID
_run_dir = None
_step = 0
_actions = []
_last_jpg = b""
_last_img = None                  # PIL image of _last_jpg, for screen change
_last_state = None                # harness-only; never sent to the model
_last_debug = ""
_milestones = []
_user_pid = None                  # app to hand focus back to (see _unfocus_game)


# ---------------------------------------------------------------- game process

def _write_config():
    """OpenMW config layered on top of the user's own: adds the mod, skips videos."""
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA.mkdir(parents=True, exist_ok=True)
    CMD_FILE.parent.mkdir(exist_ok=True)
    (CFG_DIR / "openmw.cfg").write_text("\n".join([
        "# generated by game.py - edit openmw/ templates instead",
        f'data="{MOD}"',
        "content=piagent.omwscripts",
        "fallback=Movies_Company_Logo,piagent_none.bik",
        "fallback=Movies_Morrowind_Logo,piagent_none.bik",
        "fallback=Movies_New_Game,piagent_none.bik",
        "",
    ]))
    settings = CFG_DIR / "settings.cfg"
    if not settings.exists():  # OpenMW rewrites it on exit; keep its edits
        settings.write_text((ROOT / "openmw" / "settings.cfg").read_text())


def _find_game_pid():
    out = subprocess.run(["pgrep", "-f", "--", f"--config {CFG_DIR}"], capture_output=True, text=True)
    pids = [int(p) for p in out.stdout.split()]
    return pids[0] if pids else None


def _game_alive():
    global _game_pid
    if _game_pid:
        try:
            os.kill(_game_pid, 0)
            return True
        except OSError:
            _game_pid = None
    _game_pid = _find_game_pid()
    return _game_pid is not None


def _stop_game():
    global _game, _game_pid, _window
    if _game_alive():
        os.kill(_game_pid, signal.SIGTERM)
        for _ in range(50):
            if not _game_alive():
                break
            time.sleep(0.2)
        if _game_alive():
            os.kill(_game_pid, signal.SIGKILL)
    _game, _game_pid, _window = None, None, None


def _launch_game():
    global _game, _game_pid, _window
    _write_config()
    _write_command({"op": "noop"})  # never replay a stale command on boot
    log = open(RUNS / "openmw.stdout", "ab")
    _game = subprocess.Popen(
        [OPENMW_BIN, "--config", str(CFG_DIR), "--user-data", str(USER_DATA),
         "--no-grab", "--skip-menu", "--new-game"],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    _game_pid, _window = _game.pid, None
    print(f"[game] launched OpenMW pid={_game_pid}", flush=True)


def _ensure_game(fresh=False):
    if fresh:
        _stop_game()
    if not _game_alive():
        _launch_game()
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        if not _game_alive():
            raise RuntimeError(f"OpenMW exited during startup - see {GAME_LOG}")
        try:
            return _lua({"op": "state"}, timeout=3)
        except TimeoutError:
            pass
    raise RuntimeError("OpenMW did not answer the agent mod in time")


# ------------------------------------------------------------ mod channel (Lua)

def _lua_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if v is None:
        return "nil"
    s = re.sub(r"[^\x20-\x7e]", "", str(v))
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_command(cmd):
    body = "{" + ", ".join(f"{k}={_lua_value(v)}" for k, v in cmd.items() if v is not None) + "}"
    tmp = CMD_FILE.with_suffix(".tmp")
    tmp.write_text(body)
    os.replace(tmp, CMD_FILE)


def _lua(cmd, timeout=LUA_TIMEOUT):
    """Send one command to the mod and wait for its PIAGENT reply."""
    global _seq
    _seq += 1
    seq = _seq
    _write_command({"seq": seq, "nonce": _nonce, "pause": PAUSE_BETWEEN, **cmd})
    deadline = time.time() + timeout
    with _results_cv:
        while seq not in _results:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError(f"no reply from the game for {cmd.get('op')}")
            _results_cv.wait(left)
        return _results.pop(seq)


def _tail_log():
    """Follow openmw.log (recreated on every game launch) for PIAGENT lines."""
    pos, ino = 0, None
    while True:
        try:
            st = GAME_LOG.stat()
            if st.st_ino != ino or st.st_size < pos:
                pos, ino = 0, st.st_ino
            if st.st_size > pos:
                with GAME_LOG.open("rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                end = chunk.rfind(b"\n") + 1
                pos += end
                for raw in chunk[:end].splitlines():
                    line = raw.decode("utf-8", "replace")
                    i = line.find("PIAGENT {")
                    if i < 0:
                        continue
                    try:
                        msg = json.loads(line[i + 8:])
                    except json.JSONDecodeError:
                        continue
                    with _results_cv:
                        if msg.get("nonce") == _nonce and "seq" in msg:
                            _results[msg["seq"]] = msg
                        _results_cv.notify_all()
                if "Lua error" in chunk.decode("utf-8", "replace"):
                    for raw in chunk.splitlines():
                        if b"piagent" in raw and b"rror" in raw:
                            print("[game] " + raw.decode("utf-8", "replace"), flush=True)
        except FileNotFoundError:
            pass
        time.sleep(0.03)


# ------------------------------------------------------------- screen + input

def _window_info():
    global _window
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    for w in infos:
        if w.get("kCGWindowOwnerPID") == _game_pid and w.get("kCGWindowLayer") == 0:
            b = w["kCGWindowBounds"]
            if b["Width"] > 100:
                _window = w["kCGWindowNumber"]
                return _window, (b["X"], b["Y"], b["Width"], b["Height"])
    raise RuntimeError("OpenMW window not found (minimized or still starting?)")


def _screenshot():
    wid, _ = _window_info()
    path = RUNS / "latest-capture.png"
    res = subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(path)],
                         capture_output=True, text=True)
    if res.returncode != 0 or not path.exists():
        raise RuntimeError(f"screencapture failed: {res.stderr.strip()} (Screen Recording permission?)")
    img = Image.open(path).convert("RGB")
    if img.width != MODEL_WIDTH:
        img = img.resize((MODEL_WIDTH, round(img.height * MODEL_WIDTH / img.width)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), img


def _screen_change(before, after):
    """Fraction of the screen that visibly changed, ignoring compression noise."""
    if before is None or before.size != after.size:
        return 1.0
    size = (160, 90)
    diff = ImageChops.difference(before.convert("L").resize(size), after.convert("L").resize(size))
    return sum(diff.histogram()[17:]) / (size[0] * size[1])


# Focus goes through the Accessibility API: a background process can't reliably
# activate other apps with NSRunningApplication, and NSWorkspace's view of the
# frontmost app goes stale without a run loop.

def _is_frontmost(pid):
    err, value = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFrontmost", None)
    return not err and bool(value)


def _front_window_pid():
    """Owner of the frontmost normal window (the system-wide AX focus query fails)."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowLayer") == 0:
            return w.get("kCGWindowOwnerPID")
    return None


def _set_frontmost(pid):
    AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFrontmost", True)
    deadline = time.time() + 1.0
    while not _is_frontmost(pid) and time.time() < deadline:
        time.sleep(0.03)


def _focus_game():
    """Bring OpenMW to the front for input: clicks and typed characters only
    reach the frontmost window. Remembers the user's app to hand focus back."""
    global _user_pid
    if _is_frontmost(_game_pid):
        return
    front = _front_window_pid()
    if front and front != _game_pid:
        _user_pid = front
    _set_frontmost(_game_pid)
    time.sleep(0.15)


def _unfocus_game():
    """Keep the game in the background between inputs: with the cursor
    ungrabbed, a focused OpenMW turns the camera whenever the mouse passes over
    its window."""
    if REFOCUS and _user_pid and _is_frontmost(_game_pid):
        _set_frontmost(_user_pid)


def _post_key(code, down, text=None):
    e = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if text:
        Quartz.CGEventKeyboardSetUnicodeString(e, len(text), text)
    Quartz.CGEventPostToPid(_game_pid, e)


def _tap(code, text=None, hold=0.04):
    _post_key(code, True, text)
    time.sleep(hold)
    _post_key(code, False, text)
    time.sleep(0.04)


def _press_key(name, times=1, hold=0.05, allow_forbidden=False):
    if name in FORBIDDEN_KEYS and not allow_forbidden:
        raise ValueError(f"key {name!r} is not available")
    code = KEYCODES.get(name.lower())
    if code is None:
        raise ValueError(f"unknown key {name!r}; known: {', '.join(sorted(KEYCODES))}")
    for _ in range(times):
        _tap(code, hold=hold)


def _type_text(text, allow_forbidden=False):
    if not allow_forbidden and any(ch in FORBIDDEN_KEYS for ch in text):
        raise ValueError(f"text may not contain {' '.join(sorted(FORBIDDEN_KEYS))}")
    for ch in text:
        if ch == "\n":
            _tap(KEYCODES["enter"])
        else:
            _tap(KEYCODES.get(ch.lower(), 0), text=ch)


def _click(x, y, img_size, button="left", double=False):
    """Click at (x, y) in screenshot coordinates. The caller focuses the game."""
    _, (wx, wy, ww, wh) = _window_info()
    px = wx + max(0, min(x, img_size[0] - 1)) * ww / img_size[0]
    py = wy + max(0, min(y, img_size[1] - 1)) * wh / img_size[1]
    if button == "right":
        kinds = (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp)
        btn = Quartz.kCGMouseButtonRight
    else:
        kinds = (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp)
        btn = Quartz.kCGMouseButtonLeft
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (px, py), btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.08)
    for n in range(2 if double else 1):
        for kind in kinds:
            e = Quartz.CGEventCreateMouseEvent(None, kind, (px, py), btn)
            Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, n + 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
            time.sleep(0.06)
    time.sleep(0.15)


# ------------------------------------------------- debug summary (humans only)

def _fmt_stat(s):
    return f"{s['current']}/{s['base']}" if s else "?"


def _debug_summary(state):
    """Game state for the web UI and logs. Never send this to the model."""
    if not state:
        return "(no game state)"
    cell = state.get("cell") or {}
    where = "exterior" if cell.get("exterior") else "interior"
    grid = cell.get("grid")
    modes = state.get("ui_modes") or []
    blocked = [k for k, v in (state.get("controls") or {}).items() if v is False]
    lines = [
        f"Cell: {cell.get('name') or '?'} ({where}{f' {grid[0]},{grid[1]}' if grid else ''}) | "
        f"pos {state.get('pos')} | heading {state.get('heading')}° | pitch {state.get('pitch')}°",
        "Menus open: " + (" > ".join(modes) if modes else "none"),
        f"Health {_fmt_stat(state.get('health'))}, magicka {_fmt_stat(state.get('magicka'))}, "
        f"fatigue {_fmt_stat(state.get('fatigue'))} | stance {state.get('stance')} | chargen "
        f"{'finished' if state.get('chargen_done') else 'in progress'}"
        + (f" | disabled: {', '.join(blocked)}" if blocked else ""),
        f"Crosshair: {state.get('crosshair') or 'nothing in reach'}",
    ]
    journal = state.get("journal") or []
    if journal:
        lines.append("Latest journal entries:")
        lines += [f"  - {j.get('text', '').strip()}" for j in journal]
    return "\n".join(lines)


def _check_milestones(state):
    cell = (state or {}).get("cell") or {}
    if cell.get("exterior") and "left the ship" not in _milestones:
        _milestones.append("left the ship")
        return "left the ship"
    return None


# -------------------------------------------------------------------- actions

def _observe(result, entry):
    """Screenshot after an action and record evidence for the run."""
    global _last_jpg, _last_img, _last_state, _last_debug
    time.sleep(0.12)  # let the action's last frame render
    jpg, img = _screenshot()
    state = result.get("state") if result else None
    entry["changed"] = round(_screen_change(_last_img, img), 3)
    _last_jpg, _last_img, _last_state, _last_debug = jpg, img, state, _debug_summary(state)
    milestone = _check_milestones(state)
    if milestone:
        entry["milestone"] = milestone
    if _run_dir:
        (_run_dir / "frames" / f"step-{entry['step']:04d}.jpg").write_bytes(jpg)
        with (_run_dir / "actions.jsonl").open("a") as fh:
            fh.write(json.dumps({**entry, "state": state}) + "\n")


def look():
    with _lock:
        if not _game_alive():
            raise RuntimeError("the game is not running - reset the run")
        result = _lua({"op": "state"})
        entry = {"step": _step, "action": "look", "ts": time.time()}
        _observe(result, entry)
        return {"step": _step}


def act(params):
    global _step
    action = params.get("action")
    if action not in WORLD_ACTIONS | UI_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    with _lock:
        if not _game_alive():
            raise RuntimeError("the game is not running - reset the run")
        if action in WORLD_ACTIONS:
            args = {k: params.get(k) for k in ("dir", "seconds", "run", "degrees", "pitch", "stance")}
            result = _lua({"op": action, **args})
            if result.get("outcome") == "error":  # the mod only rejects malformed arguments
                raise ValueError(result.get("note") or "invalid arguments")
        else:
            # With PI_PAUSE, gameplay keys and clicks (E, space, attacking) are ignored while
            # the world is paused, so run it during input outside menus; the wait below
            # pauses it again. Menus hold their own pause, and input into them works with ours on.
            if PAUSE_BETWEEN and not (_last_state or {}).get("ui_modes"):
                _lua({"op": "release"})
                time.sleep(0.1)
            _focus_game()
            try:
                if action == "click":
                    size = (_last_img or _screenshot()[1]).size  # coordinates refer to the model's last screenshot
                    _click(float(params["x"]), float(params["y"]), size,
                           params.get("button", "left"), bool(params.get("double")))
                elif action == "type":
                    _type_text(str(params.get("text", ""))[:200])
                elif action == "key":
                    _press_key(str(params.get("key", "")), int(params.get("times") or 1))
            except Exception:
                if PAUSE_BETWEEN:
                    _lua({"op": "pause"})
                raise
            finally:
                _unfocus_game()
            # Let the world run a moment so scripts react, then report (and pause, with PI_PAUSE).
            result = _lua({"op": "wait", "seconds": float(params.get("seconds") or 0.4)})
        _step += 1
        entry = {"step": _step, "action": action,
                 "params": {k: v for k, v in params.items() if k != "action"},
                 "outcome": result.get("outcome"), "note": result.get("note"), "ts": time.time()}
        _observe(result, entry)
        _actions.append(entry)
        return {"step": _step, "changed": entry["changed"]}


def reset(name=None, fresh=False):
    global _run_dir, _step, _actions, _milestones
    with _lock:
        result = _ensure_game(fresh)
        _unfocus_game()
        _step, _actions, _milestones = 0, [], []
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", name or time.strftime("run-%Y%m%d-%H%M%S"))
        _run_dir = RUNS / safe
        (_run_dir / "frames").mkdir(parents=True, exist_ok=True)
        (_run_dir / "actions.jsonl").write_text("")
        (_run_dir / "run.json").write_text(json.dumps({
            "started_at": time.time(), "fresh_game": fresh, "openmw": OPENMW_BIN}, indent=2))
        _observe(result, {"step": 0, "action": "reset", "ts": time.time()})
        return {"run": _run_dir.name, "step": 0}


def release():
    with _lock:
        return _lua({"op": "release"}).get("outcome")


def _check_lua_syntax():
    """Refuse to reload broken scripts: a syntax error leaves the mod dead
    until the next reload. Skipped when luajit isn't installed."""
    luajit = shutil.which("luajit")
    if not luajit:
        return
    for path in sorted((MOD / "scripts").rglob("*.lua")):
        res = subprocess.run([luajit, "-bl", str(path)], capture_output=True, text=True)
        if res.returncode != 0:
            raise ValueError(f"Lua syntax error, not reloading: {res.stderr.strip()}")


def reload_lua():
    """Dev helper: re-run the mod's scripts via the in-game console. Edited
    files are picked up; files created after the game started are not."""
    _check_lua_syntax()
    with _lock:
        if not _game_alive():
            raise RuntimeError("the game is not running")
        _press_key("`", allow_forbidden=True)
        time.sleep(0.5)
        _type_text("reloadlua")
        _press_key("enter")
        time.sleep(0.8)
        _press_key("`", allow_forbidden=True)
        return _lua({"op": "state"}, timeout=10).get("outcome")


# ---------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/screen.jpg":
                self._send(200, _last_jpg, "image/jpeg")
            elif url.path == "/status":
                self._json({"run": _run_dir.name if _run_dir else None, "step": _step,
                            "game_running": _game_alive(), "milestones": _milestones,
                            "debug": _last_debug, "last": _actions[-1] if _actions else None})
            elif url.path == "/log":
                self._json({"run": _run_dir.name if _run_dir else None, "actions": list(_actions)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path == "/act":
                self._json(act(self._body()))
            elif url.path == "/look":
                self._json(look())
            elif url.path == "/reset":
                q = parse_qs(url.query)
                fresh = q.get("fresh", ["0"])[0] in ("1", "true", "yes")
                self._json(reset(q.get("name", [None])[0], fresh=fresh))
            elif url.path == "/release":
                self._json({"outcome": release()})
            elif url.path == "/reload-lua":
                self._json({"outcome": reload_lua()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)

    def log_message(self, *args):
        pass


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>pi plays Morrowind</title>
<style>
 body { background:#15120e; color:#ddd; font:13px/1.5 ui-monospace, monospace; margin:16px; }
 #status { margin:8px 0; color:#e8c872; }
 img { width:768px; border:1px solid #3a3226; display:block; }
 pre { white-space:pre-wrap; margin:8px 0; color:#8a8170; max-width:768px; }
 table { border-collapse:collapse; width:768px; margin-top:8px; }
 td, th { padding:1px 8px; text-align:left; border-bottom:1px solid #2a241c; }
 th { color:#e8c872; font-weight:normal; }
 td.n { text-align:right; }
 tr.ms td { color:#9fd49f; }
</style>
<div id="status">connecting...</div>
<img id="screen" src="/screen.jpg">
<table><thead><tr><th>step</th><th>action</th><th>args</th><th>change</th><th>outcome (log only)</th></tr></thead>
<tbody id="steps"></tbody></table>
<pre id="state"></pre>
<script>
const esc = s => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;");
const args = p => Object.entries(p || {}).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : v}`).join(" ");
async function tick() {
  try {
    const s = await (await fetch("/status")).json();
    document.getElementById("status").textContent =
      `run ${s.run} | step ${s.step} | game ${s.game_running ? "running" : "not running"}` +
      (s.milestones.length ? ` | milestones: ${s.milestones.join(", ")}` : "");
    document.getElementById("state").textContent = "debug state (not shown to the model)\\n" + s.debug;
    document.getElementById("screen").src = "/screen.jpg?t=" + Date.now();
    const log = await (await fetch("/log")).json();
    document.getElementById("steps").innerHTML = log.actions.slice(-25).reverse().map(e =>
      `<tr class="${e.milestone ? "ms" : ""}"><td class="n">${e.step}</td><td>${esc(e.action)}</td>` +
      `<td>${esc(args(e.params))}</td><td class="n">${(100 * (e.changed ?? 0)).toFixed(1)}%</td>` +
      `<td>${esc(e.outcome)} ${esc(e.note)}${e.milestone ? " | " + esc(e.milestone) : ""}</td></tr>`).join("");
  } catch (e) { document.getElementById("status").textContent = "offline: " + e; }
}
setInterval(tick, 700); tick();
</script>
"""


def main():
    global _user_pid
    front = _front_window_pid()
    if front and front != _find_game_pid():
        _user_pid = front
    else:
        finder = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.finder")
        _user_pid = finder[0].processIdentifier() if finder else None
    RUNS.mkdir(exist_ok=True)
    threading.Thread(target=_tail_log, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[game] http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
