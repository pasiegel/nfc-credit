"""
NFC Credit - tap an NFC card on a PN532 (USB/CH340 serial) reader and do
something: press keys (default "5" = coin), run a program, or ignore it.

Runs in the system tray. Right-click the icon for Settings / Test mode / Quit.
Behaviour is stored in nfc_credit.toml next to the exe (Settings > Save writes
it; hand edits are picked up automatically too).

    NFC-Credit.exe            # tray app
    NFC-Credit.exe --test     # start in test mode (log taps, don't act)
    NFC-Credit.exe --settings # open the settings window on start

Close NFC Tools first - only one program can hold the COM port.
"""
import argparse
import collections
import ctypes
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
import tomllib
import winreg
from ctypes import wintypes

import serial
import serial.tools.list_ports

APP_NAME = "NFC Credit"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))

DEFAULT_CONFIG = '''# NFC Credit config - edit with Settings in the tray menu, or by hand.
[reader]
port = "auto"
remove_time = 0.6
poll_interval = 0.05
card_types = ["type_a", "felica"]   # also: "type_b", "jewel"

[options]
log_file = ""

[default]
action = "keys"
keys = ["5"]
count = 1
'''

# --------------------------------------------------------------------------
# Keyboard (SendInput with hardware scan codes - games/MAME see these)
# --------------------------------------------------------------------------
EXT = 0x100  # marker: extended key
SCANCODES = {
    "esc": 0x01, "minus": 0x0C, "equals": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "enter": 0x1C, "ctrl": 0x1D, "lctrl": 0x1D, "rctrl": 0x1D | EXT,
    "shift": 0x2A, "lshift": 0x2A, "rshift": 0x36,
    "alt": 0x38, "lalt": 0x38, "ralt": 0x38 | EXT, "space": 0x39,
    "up": 0x48 | EXT, "down": 0x50 | EXT, "left": 0x4B | EXT, "right": 0x4D | EXT,
    "insert": 0x52 | EXT, "delete": 0x53 | EXT, "home": 0x47 | EXT,
    "end": 0x4F | EXT, "pageup": 0x49 | EXT, "pagedown": 0x51 | EXT,
    "numpadenter": 0x1C | EXT,
    "numpad7": 0x47, "numpad8": 0x48, "numpad9": 0x49, "numpad4": 0x4B,
    "numpad5": 0x4C, "numpad6": 0x4D, "numpad1": 0x4F, "numpad2": 0x50,
    "numpad3": 0x51, "numpad0": 0x52,
}
for i, ch in enumerate("1234567890"):
    SCANCODES[ch] = 0x02 + i
for i, ch in enumerate("qwertyuiop"):
    SCANCODES[ch] = 0x10 + i
for i, ch in enumerate("asdfghjkl"):
    SCANCODES[ch] = 0x1E + i
for i, ch in enumerate("zxcvbnm"):
    SCANCODES[ch] = 0x2C + i
for i in range(10):
    SCANCODES[f"f{i + 1}"] = 0x3B + i
SCANCODES["f11"], SCANCODES["f12"] = 0x57, 0x58

# Mouse buttons ride along as pseudo key codes (click at current cursor position)
MOUSE = 0x1000
MOUSE_FLAGS = {  # code: (down flag, up flag)
    MOUSE | 1: (0x0002, 0x0004),   # left
    MOUSE | 2: (0x0008, 0x0010),   # right
    MOUSE | 3: (0x0020, 0x0040),   # middle
}
SCANCODES.update({"lclick": MOUSE | 1, "rclick": MOUSE | 2, "mclick": MOUSE | 3})

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(code, up):
    if code & MOUSE:
        flags = MOUSE_FLAGS[code][1 if up else 0]
        inp = INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        return
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if code & EXT:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, code & 0xFF, flags, 0, 0)))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def parse_combo(text):
    parts = [p.strip().lower() for p in str(text).split("+")]
    bad = [p for p in parts if p not in SCANCODES]
    if bad:
        raise ValueError(f"unknown key '{bad[0]}' in '{text}'")
    return [SCANCODES[p] for p in parts]


def press_combo(codes, hold):
    for c in codes:
        _send(c, False)
    time.sleep(hold)
    for c in reversed(codes):
        _send(c, True)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def norm_uid(uid):
    return str(uid).upper().replace(":", "").replace(" ", "").replace("-", "")


def check_action(act, where):
    act = dict(act)
    kind = act.get("action", "keys")
    if kind == "keys":
        keys = act.get("keys", ["5"])
        if isinstance(keys, str):
            keys = [keys]
        if not keys:
            raise ValueError(f"{where}: no keys given")
        act["keys"] = keys
        act["_combos"] = [parse_combo(k) for k in keys]
        if int(act.get("count", 1)) < 1:
            raise ValueError(f"{where}: count must be 1 or more")
    elif kind == "run":
        if not act.get("command"):
            raise ValueError(f"{where}: action 'run' needs a command")
    elif kind != "ignore":
        raise ValueError(f"{where}: unknown action '{kind}'")
    act["action"] = kind
    return act


CARD_TYPES = {  # key: (label, hint)
    "type_a": ("Type A", "Bandai Namco Passport, older Aime/e-amusement, most NFC stickers & blank cards, amiibo"),
    "felica": ("FeliCa", "newer Aime/e-amusement (Amusement IC), Suica/PASMO, Octopus"),
    "type_b": ("Type B", "some ID cards, transit & building passes"),
    "jewel": ("Jewel/Topaz", "rare NFC Type 1 tags"),
}
DEFAULT_TYPES = ["type_a", "felica"]


def parse_config(data):
    data = dict(data)
    data.setdefault("reader", {})
    types = data["reader"].get("card_types", DEFAULT_TYPES)
    if isinstance(types, str):
        types = [types]
    bad = [t for t in types if t not in CARD_TYPES]
    if bad:
        raise ValueError(f"unknown card type '{bad[0]}' (use {', '.join(CARD_TYPES)})")
    if not types:
        raise ValueError("at least one card type must be enabled")
    data["reader"]["card_types"] = [t for t in CARD_TYPES if t in types]
    data.setdefault("options", {})
    data["default"] = check_action(data.get("default", {}), "Default action")
    cards = {}
    for uid, act in data.get("cards", {}).items():
        label = act.get("name") or uid
        cards[norm_uid(uid)] = check_action(act, f"Card {label}")
    data["cards"] = cards
    return data


def _tv(v):
    """Python value -> TOML literal (JSON strings are valid TOML basic strings)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_tv(x) for x in v) + "]"
    return json.dumps(str(v))


def _table(name, d):
    lines = [f"[{name}]"]
    for k, v in d.items():
        if not k.startswith("_"):
            lines.append(f"{k} = {_tv(v)}")
    return "\n".join(lines)


def dump_config(data):
    out = ["# NFC Credit config - written by Settings > Save. Hand edits are fine too.",
           "# actions: keys (keys = [\"5\", \"shift+f1\"], count, hold) | run (command) | ignore",
           "# mouse buttons work as keys: lclick, rclick, mclick (at the current cursor)",
           "",
           _table("reader", data["reader"]), "",
           _table("options", data["options"]), "",
           "# Any card that isn't listed below",
           _table("default", data["default"])]
    for uid, act in data["cards"].items():
        out += ["", _table(f'cards."{uid}"', act)]
    return "\n".join(out) + "\n"


class Config:
    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.data = parse_config(tomllib.loads(DEFAULT_CONFIG))

    def load(self):
        with open(self.path, "rb") as f:
            data = parse_config(tomllib.load(f))
        self.data = data
        self.mtime = os.path.getmtime(self.path)

    def save(self, data):
        text = dump_config(data)
        parsed = parse_config(tomllib.loads(text))      # validate round-trip
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)
        self.data = parsed
        self.mtime = os.path.getmtime(self.path)

    def changed(self):
        try:
            return os.path.getmtime(self.path) != self.mtime
        except OSError:
            return False

    def action_for(self, uid):
        return self.data["cards"].get(uid, self.data["default"])


def describe(act):
    kind = act["action"]
    if kind == "keys":
        n = int(act.get("count", 1))
        return f"keys {' '.join(act['keys'])}" + (f" x{n}" if n > 1 else "")
    if kind == "run":
        return f"run {act['command']}"
    return "ignore"


# --------------------------------------------------------------------------
# PN532 over HSU (UART) - minimal driver
# --------------------------------------------------------------------------
class PN532:
    def __init__(self, port, baud=115200):
        self.ser = serial.Serial(port, baud, timeout=0.5)
        try:
            self._wake()
            self.command(0x32, b"\x05\xff\x01\x02")      # quick-fail polling
        except Exception:
            self.close()
            raise

    def _wake(self, attempts=3):
        """Wake the PN532 from power-on/low-power state. The wake preamble must be
        followed immediately by the first command (no pause), or a freshly powered
        board goes back to sleep and ignores it."""
        sam = self._frame(0x14, b"\x01\x14\x01")         # SAMConfiguration
        for i in range(attempts):
            self.ser.reset_input_buffer()
            self.ser.write(b"\x55\x55" + b"\x00" * 14 + sam)
            try:
                self._finish(0x14)
                return
            except (TimeoutError, IOError):
                if i == attempts - 1:
                    raise
                self.ser.write(b"\x00\x00\xff\x00\xff\x00")  # ACK = abort anything pending
                time.sleep(0.1)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    @staticmethod
    def _frame(cmd, data):
        body = bytes([0xD4, cmd]) + data
        ln = len(body)
        return (b"\x00\x00\xff" + bytes([ln, (-ln) & 0xFF]) + body
                + bytes([(-sum(body)) & 0xFF, 0x00]))

    def _read_exact(self, n):
        buf = self.ser.read(n)
        if len(buf) != n:
            raise TimeoutError("PN532 read timeout")
        return buf

    def _read_frame(self):
        prev, deadline = None, time.time() + 1.5
        while time.time() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            if prev == 0x00 and b[0] == 0xFF:
                break
            prev = b[0]
        else:
            raise TimeoutError("PN532 not responding")
        ln, lcs = self._read_exact(2)
        if ln == 0 and lcs == 0xFF:                      # ACK
            self._read_exact(1)
            return None
        if (ln + lcs) & 0xFF:
            raise IOError("PN532 bad length checksum")
        body = self._read_exact(ln)
        self._read_exact(2)
        return body

    def command(self, cmd, data=b""):
        self.ser.write(self._frame(cmd, data))
        return self._finish(cmd)

    def _finish(self, cmd):
        """Read the ACK and response for a command already sent."""
        if self._read_frame() is not None:
            raise IOError("PN532 expected ACK")
        body = self._read_frame()
        if not body or body[0] != 0xD5 or body[1] != cmd + 1:
            raise IOError(f"PN532 bad response {body!r}")
        return body[2:]

    def firmware(self):
        r = self.command(0x02)
        return f"PN5{r[0]:02X} v{r[1]}.{r[2]}"

    # InListPassiveTarget payloads (MaxTg=1, BrTy, initiator data) and ID extractors
    # r = [NbTg, Tg, target data...]
    POLLS = {
        # ISO14443A 106k: SENS_RES(2) SEL_RES(1) UIDLen(1) UID
        "type_a": (b"\x01\x00", lambda r: r[6:6 + r[5]]),
        # FeliCa 212k, polling sys code FFFF: POL_RES len(1) 01 IDm(8) PMm(8)
        "felica": (b"\x01\x01\x00\xff\xff\x01\x00", lambda r: r[4:12]),
        # ISO14443B 106k, AFI 00 (all): ATQB = 50 PUPI(4) ...
        "type_b": (b"\x01\x03\x00", lambda r: r[3:7]),
        # Innovision Jewel/Topaz (NFC Type 1): SENS_RES(2) JEWELID(4)
        "jewel": (b"\x01\x04", lambda r: r[4:8]),
    }

    def poll(self, types=("type_a", "felica")):
        """Card ID hex string if a card is present, else None."""
        for t in types:
            data, get_id = self.POLLS[t]
            r = self.command(0x4A, data)
            if r and r[0] >= 1:
                uid = get_id(r)
                if uid:
                    return uid.hex().upper()
        return None


def open_reader(port_setting):
    """'auto' probes USB serial ports (CH340 first) and uses the first that
    answers as a PN532, so COM number changes / other serial devices don't matter."""
    if port_setting.lower() != "auto":
        return PN532(port_setting), port_setting
    ports = [p for p in serial.tools.list_ports.comports() if p.vid is not None]
    ports.sort(key=lambda p: p.vid != 0x1A86)
    for p in ports:
        try:
            return PN532(p.device), p.device
        except (serial.SerialException, TimeoutError, IOError, OSError):
            pass
    raise serial.SerialException("no PN532 reader found on any USB serial port")


# --------------------------------------------------------------------------
# Reader thread
# --------------------------------------------------------------------------
class Reconnect(Exception):
    pass


class Reader(threading.Thread):
    """Polls the reader and runs actions. Talks to the GUI through `events`:
       ("log", text) ("status", connected, text) ("tap", uid)"""

    def __init__(self, cfg, test=False):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.test = test
        self.events = queue.Queue()
        self.stop_evt = threading.Event()
        self.last_uid = None
        self.connected = False
        self.status_text = "Searching for reader..."
        self.log_lines = collections.deque(maxlen=300)
        self._log_fh = None
        self._log_name = None

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_lines.append(line)
        self.events.put(("log", line))
        name = self.cfg.data["options"].get("log_file", "")
        if name != self._log_name:
            if self._log_fh:
                self._log_fh.close()
                self._log_fh = None
            self._log_name = name
            if name:
                try:
                    self._log_fh = open(os.path.join(APP_DIR, name), "a", encoding="utf-8")
                except OSError:
                    pass
        if self._log_fh:
            self._log_fh.write(time.strftime("%Y-%m-%d ") + line + "\n")
            self._log_fh.flush()

    def set_status(self, connected, text):
        self.connected, self.status_text = connected, text
        self.events.put(("status", connected, text))

    def do_action(self, act):
        kind = act["action"]
        if self.test or kind == "ignore":
            return
        if kind == "keys":
            hold = float(act.get("hold", 0.08))
            for _ in range(int(act.get("count", 1))):
                for combo in act["_combos"]:
                    press_combo(combo, hold)
                    time.sleep(0.1)
        elif kind == "run":
            try:
                cmd = act["command"]
                subprocess.Popen(cmd if isinstance(cmd, list)
                                 else shlex.split(cmd, posix=False),
                                 cwd=act.get("cwd") or None)
            except OSError as e:
                self.log(f"  run failed: {e}")

    def check_reload(self, port_setting):
        if not self.cfg.changed():
            return
        try:
            self.cfg.load()
            self.log(f"Config loaded: {len(self.cfg.data['cards'])} mapped card(s), "
                     f"default {describe(self.cfg.data['default'])}")
        except (ValueError, tomllib.TOMLDecodeError, OSError) as e:
            self.log(f"Config error, keeping previous settings: {e}")
            try:
                self.cfg.mtime = os.path.getmtime(self.cfg.path)
            except OSError:
                pass
        if str(self.cfg.data["reader"].get("port", "auto")) != port_setting:
            raise Reconnect()

    def run(self):
        pn = None
        last_err = None
        while not self.stop_evt.is_set():
            port_setting = str(self.cfg.data["reader"].get("port", "auto"))
            try:
                self.check_reload(port_setting)
                pn, port = open_reader(port_setting)
                fw = pn.firmware()
                last_err = None
                self.set_status(True, f"{fw} on {port}")
                self.log(f"Reader {fw} on {port}, card types: "
                         + ", ".join(CARD_TYPES[t][0]
                                     for t in self.cfg.data["reader"]["card_types"]))
                present, last_seen = None, 0.0
                while not self.stop_evt.is_set():
                    self.check_reload(port_setting)
                    rd = self.cfg.data["reader"]
                    uid = pn.poll(rd["card_types"])
                    now = time.time()
                    if uid:
                        if present is None:
                            act = self.cfg.action_for(uid)
                            name = act.get("name") or (
                                "mapped" if uid in self.cfg.data["cards"] else "unlisted")
                            self.last_uid = uid
                            self.log(f"tap {uid} ({name}) -> {describe(act)}"
                                     + ("  [test mode]" if self.test else ""))
                            self.events.put(("tap", uid))
                            self.do_action(act)
                        present, last_seen = uid, now
                    elif present and now - last_seen > float(rd.get("remove_time", 0.6)):
                        present = None
                    self.stop_evt.wait(float(rd.get("poll_interval", 0.05)))
            except Reconnect:
                self.log("Port setting changed - reconnecting")
            except (serial.SerialException, TimeoutError, IOError, OSError) as e:
                if str(e) != last_err:
                    self.log(f"Reader: {e} - retrying")
                    last_err = str(e)
                self.set_status(False, "Searching for reader...")
                self.stop_evt.wait(3)
            finally:
                if pn:
                    pn.close()
                    pn = None
        if self._log_fh:
            self._log_fh.close()


# --------------------------------------------------------------------------
# Start with Windows (HKCU Run key)
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command():
    if FROZEN:
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def get_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Icon
# --------------------------------------------------------------------------
COLORS = {"ok": "#2e9e44", "search": "#808080", "tap": "#f0a020", "test": "#7a4fd0"}


def make_icon(state="ok", size=64):
    from PIL import Image, ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((1, 1, s - 2, s - 2), fill=COLORS[state])
    # contactless waves
    w = max(2, s // 14)
    cx, cy = int(s * 0.30), s // 2
    for r in (s * 0.16, s * 0.29, s * 0.42):
        d.arc((cx - r, cy - r, cx + r, cy + r), -50, 50, fill="white", width=w)
    d.ellipse((cx - w, cy - w, cx + w, cy + w), fill="white")
    return img


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    import pystray
    from PIL import ImageTk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    cfg_path = args.config
    first_run = not os.path.exists(cfg_path)
    if first_run:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG)
    cfg = Config(cfg_path)
    load_error = None
    try:
        cfg.load()
    except (ValueError, tomllib.TOMLDecodeError) as e:
        load_error = str(e)
        cfg.mtime = os.path.getmtime(cfg_path)   # don't retry the bad file

    reader = Reader(cfg, test=args.test)
    root = tk.Tk()
    root.withdraw()
    root.title(APP_NAME)
    tk_icon = ImageTk.PhotoImage(make_icon("ok", 32))
    root.iconphoto(True, tk_icon)
    ui_q = queue.Queue()          # callables from the tray thread
    state = {"win": None, "flash": None}

    def icon_state():
        if not reader.connected:
            return "search"
        return "test" if reader.test else "ok"

    def tooltip():
        t = f"{APP_NAME} - {reader.status_text}"
        return (t + " [TEST MODE]")[:127] if reader.test else t[:127]

    def refresh_icon(override=None):
        tray.icon = make_icon(override or icon_state())
        tray.title = tooltip()

    # ---------------- Settings window ----------------
    class ActionEditor(ttk.Frame):
        def __init__(self, master):
            super().__init__(master)
            self.extra = {}
            self.action = tk.StringVar(value="keys")
            self.keys = tk.StringVar(value="5")
            self.count = tk.StringVar(value="1")
            self.command = tk.StringVar()
            ttk.Label(self, text="Action").grid(row=0, column=0, sticky="w", padx=(0, 6))
            cb = ttk.Combobox(self, textvariable=self.action, width=8, state="readonly",
                              values=["keys", "run", "ignore"])
            cb.grid(row=0, column=1, sticky="w")
            cb.bind("<<ComboboxSelected>>", lambda e: self._sync())
            ttk.Label(self, text="Keys").grid(row=0, column=2, sticky="e", padx=(12, 6))
            self.keys_e = ttk.Entry(self, textvariable=self.keys, width=18)
            self.keys_e.grid(row=0, column=3, sticky="w")
            ttk.Label(self, text="Count").grid(row=0, column=4, sticky="e", padx=(12, 6))
            self.count_e = ttk.Spinbox(self, from_=1, to=20, textvariable=self.count, width=4)
            self.count_e.grid(row=0, column=5, sticky="w")
            ttk.Label(self, text="e.g. 5, shift+f1, lclick, rclick",
                      foreground="#777").grid(row=0, column=6, sticky="w", padx=(12, 0))
            ttk.Label(self, text="Command").grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.cmd_e = ttk.Entry(self, textvariable=self.command)
            self.cmd_e.grid(row=1, column=1, columnspan=5, sticky="we", pady=(6, 0))
            self.browse = ttk.Button(self, text="Browse...", command=self._browse)
            self.browse.grid(row=1, column=6, padx=(6, 0), pady=(6, 0))
            self.columnconfigure(3, weight=1)
            self._sync()

        def _browse(self):
            p = filedialog.askopenfilename(parent=self, title="Program to run",
                                           filetypes=[("Programs", "*.exe *.bat *.cmd *.lnk"),
                                                      ("All files", "*.*")])
            if p:
                p = os.path.normpath(p)
                self.command.set(f'"{p}"' if " " in p else p)

        def _sync(self):
            a = self.action.get()
            for w in (self.keys_e, self.count_e):
                w.configure(state="normal" if a == "keys" else "disabled")
            for w in (self.cmd_e, self.browse):
                w.configure(state="normal" if a == "run" else "disabled")

        def set(self, act):
            self.extra = {k: v for k, v in act.items()
                          if k not in ("action", "keys", "count", "command", "name")
                          and not k.startswith("_")}
            self.action.set(act.get("action", "keys"))
            keys = act.get("keys", ["5"])
            self.keys.set(", ".join([keys] if isinstance(keys, str) else keys))
            self.count.set(str(act.get("count", 1)))
            self.command.set(act.get("command", ""))
            self._sync()

        def get(self, where):
            a = self.action.get()
            act = {"action": a}
            if a == "keys":
                act["keys"] = [k.strip() for k in self.keys.get().split(",") if k.strip()]
                try:
                    act["count"] = int(self.count.get())
                except ValueError:
                    raise ValueError(f"{where}: count must be a number")
            elif a == "run":
                act["command"] = self.command.get().strip()
            act.update(self.extra)
            check_action(act, where)       # raises ValueError
            return act

    def open_settings():
        if state["win"] is not None:
            w = state["win"]
            w.deiconify()
            w.lift()
            w.focus_force()
            return
        data = cfg.data
        cards = {uid: dict(a) for uid, a in data["cards"].items()}

        w = tk.Toplevel(root)
        state["win"] = w
        w.title(f"{APP_NAME} - Settings")
        w.minsize(640, 560)
        pad = {"padx": 10, "pady": 6}

        # Reader
        fr = ttk.LabelFrame(w, text="Reader")
        fr.pack(fill="x", **pad)
        port_v = tk.StringVar(value=str(data["reader"].get("port", "auto")))
        ports = ["auto"] + [p.device for p in serial.tools.list_ports.comports()]
        ttk.Label(fr, text="Port").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(fr, textvariable=port_v, values=ports, width=10).grid(
            row=0, column=1, sticky="w")
        rt_v = tk.StringVar(value=str(data["reader"].get("remove_time", 0.6)))
        ttk.Label(fr, text="Re-tap delay (s)").grid(row=0, column=2, padx=(16, 6))
        ttk.Spinbox(fr, from_=0.1, to=10, increment=0.1, textvariable=rt_v,
                    width=6).grid(row=0, column=3, sticky="w")
        pi_v = tk.StringVar(value=str(data["reader"].get("poll_interval", 0.05)))
        ttk.Label(fr, text="Poll every (s)").grid(row=0, column=4, padx=(16, 6))
        ttk.Spinbox(fr, from_=0.02, to=2, increment=0.05, textvariable=pi_v,
                    width=6).grid(row=0, column=5, sticky="w")
        status_l = ttk.Label(fr, text="")
        status_l.grid(row=2, column=0, columnspan=8, sticky="w", padx=8, pady=(0, 6))

        ttk.Label(fr, text="Card types").grid(row=1, column=0, sticky="nw", padx=8, pady=4)
        ct = ttk.Frame(fr)
        ct.grid(row=1, column=1, columnspan=7, sticky="w", pady=4)
        type_vars = {}
        for i, (key, (label, hint)) in enumerate(CARD_TYPES.items()):
            v = tk.BooleanVar(value=key in data["reader"]["card_types"])
            type_vars[key] = v
            ttk.Checkbutton(ct, text=label, variable=v).grid(row=i, column=0, sticky="w")
            ttk.Label(ct, text=hint, foreground="#777").grid(row=i, column=1, sticky="w",
                                                            padx=(8, 0))

        # Default action
        fd = ttk.LabelFrame(w, text="Default action (any card not listed below)")
        fd.pack(fill="x", **pad)
        def_ed = ActionEditor(fd)
        def_ed.pack(fill="x", padx=8, pady=6)
        def_ed.set(data["default"])

        # Cards
        fc = ttk.LabelFrame(w, text="Mapped cards")
        fc.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(fc, columns=("uid", "name", "does"), show="headings", height=5)
        for col, txt, wd in (("uid", "Card ID", 150), ("name", "Name", 160),
                             ("does", "Does", 260)):
            tree.heading(col, text=txt)
            tree.column(col, width=wd, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        ed = ttk.Frame(fc)
        ed.pack(fill="x", padx=8, pady=6)
        uid_v, name_v = tk.StringVar(), tk.StringVar()
        ttk.Label(ed, text="Card ID").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(ed, textvariable=uid_v, width=20).grid(row=0, column=1, sticky="w")

        def use_last():
            if reader.last_uid:
                uid_v.set(reader.last_uid)
                if reader.last_uid in cards:
                    tree.selection_set(reader.last_uid)
            else:
                messagebox.showinfo(APP_NAME, "Tap a card on the reader first.", parent=w)

        ttk.Button(ed, text="Use last tapped card", command=use_last).grid(
            row=0, column=2, padx=6)
        ttk.Label(ed, text="Name").grid(row=0, column=3, sticky="e", padx=(10, 6))
        ttk.Entry(ed, textvariable=name_v, width=22).grid(row=0, column=4, sticky="we")
        ed.columnconfigure(4, weight=1)
        card_ed = ActionEditor(ed)
        card_ed.grid(row=1, column=0, columnspan=5, sticky="we", pady=(6, 0))

        def fill_tree():
            tree.delete(*tree.get_children())
            for uid, a in cards.items():
                try:
                    does = describe(check_action(a, uid))
                except ValueError:
                    does = "?"
                tree.insert("", "end", iid=uid, values=(uid, a.get("name", ""), does))

        def on_select(_e=None):
            sel = tree.selection()
            if sel:
                a = cards[sel[0]]
                uid_v.set(sel[0])
                name_v.set(a.get("name", ""))
                card_ed.set(a)

        tree.bind("<<TreeviewSelect>>", on_select)

        def add_update():
            uid = norm_uid(uid_v.get())
            if not uid or any(c not in "0123456789ABCDEF" for c in uid):
                messagebox.showerror(APP_NAME, "Card ID should be hex, e.g. 5030DFF3.", parent=w)
                return False
            try:
                act = card_ed.get(f"Card {uid}")
            except ValueError as e:
                messagebox.showerror(APP_NAME, str(e), parent=w)
                return False
            if name_v.get().strip():
                act = {"name": name_v.get().strip(), **act}
            cards[uid] = act
            fill_tree()
            tree.selection_set(uid)
            return True

        def remove():
            for uid in tree.selection():
                cards.pop(uid, None)
            fill_tree()
            uid_v.set("")
            name_v.set("")

        def new():
            tree.selection_remove(*tree.selection())
            uid_v.set("")
            name_v.set("")
            card_ed.set({"action": "keys", "keys": ["5"], "count": 1})

        bf = ttk.Frame(fc)
        bf.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(bf, text="Add / Update card", command=add_update).pack(side="left")
        ttk.Button(bf, text="Remove", command=remove).pack(side="left", padx=6)
        ttk.Button(bf, text="Clear", command=new).pack(side="left")
        fill_tree()
        new()

        # Options
        fo = ttk.LabelFrame(w, text="Options")
        fo.pack(fill="x", **pad)
        startup_v = tk.BooleanVar(value=get_startup())
        test_v = tk.BooleanVar(value=reader.test)
        log_v = tk.StringVar(value=data["options"].get("log_file", ""))
        ttk.Checkbutton(fo, text="Start with Windows", variable=startup_v).grid(
            row=0, column=0, sticky="w", padx=8, pady=6)

        def toggle_test():
            reader.test = test_v.get()
            reader.log(f"Test mode {'ON - taps are logged, no actions' if reader.test else 'off'}")
            refresh_icon()
            tray.update_menu()

        ttk.Checkbutton(fo, text="Test mode (log taps, don't press keys)",
                        variable=test_v, command=toggle_test).grid(row=0, column=1, sticky="w",
                                                                   padx=8)
        ttk.Label(fo, text="Log file").grid(row=0, column=2, padx=(16, 6))
        ttk.Entry(fo, textvariable=log_v, width=18).grid(row=0, column=3, sticky="w")

        # Activity
        fl = ttk.LabelFrame(w, text="Activity")
        fl.pack(fill="both", expand=True, **pad)
        log_t = tk.Text(fl, height=7, wrap="none", font=("Consolas", 9))
        log_t.pack(fill="both", expand=True, padx=8, pady=6)
        log_t.insert("end", "\n".join(reader.log_lines) + "\n")
        log_t.see("end")
        log_t.configure(state="disabled")

        # Buttons
        fb = ttk.Frame(w)
        fb.pack(fill="x", padx=10, pady=(0, 10))
        saved_l = ttk.Label(fb, text="")
        saved_l.pack(side="left")

        def close():
            state["win"] = None
            w.destroy()

        def save():
            # A card typed into the editor counts even if Add / Update wasn't clicked
            if uid_v.get().strip() and not add_update():
                return
            try:
                rt = float(rt_v.get())
                pi = float(pi_v.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "Re-tap delay and poll time must be numbers.",
                                     parent=w)
                return
            if pi < 0.01:
                messagebox.showerror(APP_NAME, "Poll time must be at least 0.01 s.", parent=w)
                return
            types = [k for k, v in type_vars.items() if v.get()]
            if not types:
                messagebox.showerror(APP_NAME, "Enable at least one card type.", parent=w)
                return
            try:
                new_data = {
                    "reader": {**cfg.data["reader"], "port": port_v.get().strip() or "auto",
                               "remove_time": rt, "poll_interval": pi, "card_types": types},
                    "options": {**cfg.data["options"], "log_file": log_v.get().strip()},
                    "default": def_ed.get("Default action"),
                    "cards": cards,
                }
                cfg.save(new_data)
            except (ValueError, tomllib.TOMLDecodeError) as e:
                messagebox.showerror(APP_NAME, str(e), parent=w)
                return
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Couldn't write config:\n{e}", parent=w)
                return
            try:
                if startup_v.get() != get_startup():
                    set_startup(startup_v.get())
            except OSError as e:
                messagebox.showwarning(APP_NAME, f"Couldn't change startup setting:\n{e}",
                                       parent=w)
            reader.log(f"Settings saved: {len(cards)} mapped card(s), "
                       f"default {describe(cfg.data['default'])}")
            saved_l.configure(text=f"Saved {time.strftime('%H:%M:%S')} - {len(cards)} mapped "
                                   f"card(s), default: {describe(cfg.data['default'])}")

        ttk.Button(fb, text="Close", command=close).pack(side="right")
        ttk.Button(fb, text="Save", command=save).pack(side="right", padx=6)
        w.protocol("WM_DELETE_WINDOW", close)

        def status_update():
            status_l.configure(text=("● " if reader.connected else "○ ") + reader.status_text)

        w._status_update = status_update
        w._log_widget = log_t
        status_update()
        w.lift()
        w.focus_force()

    # ---------------- Event pump ----------------
    def pump():
        while True:
            try:
                fn = ui_q.get_nowait()
            except queue.Empty:
                break
            fn()
        while True:
            try:
                ev = reader.events.get_nowait()
            except queue.Empty:
                break
            w = state["win"]
            if ev[0] == "status":
                refresh_icon()
                if w is not None:
                    w._status_update()
            elif ev[0] == "tap":
                refresh_icon("tap")
                if state["flash"]:
                    root.after_cancel(state["flash"])
                state["flash"] = root.after(500, refresh_icon)
            elif ev[0] == "log" and w is not None:
                t = w._log_widget
                t.configure(state="normal")
                t.insert("end", ev[1] + "\n")
                t.see("end")
                t.configure(state="disabled")
        root.after(100, pump)

    # ---------------- Tray ----------------
    def from_tray(fn):
        return lambda icon=None, item=None: ui_q.put(fn)

    def toggle_test_tray():
        reader.test = not reader.test
        reader.log(f"Test mode {'ON - taps are logged, no actions' if reader.test else 'off'}")
        refresh_icon()
        tray.update_menu()

    def quit_app():
        reader.stop_evt.set()
        tray.stop()
        root.quit()

    def open_config_file():
        os.startfile(cfg_path)

    tray = pystray.Icon(
        "nfc_credit", make_icon("search"), f"{APP_NAME} - starting",
        menu=pystray.Menu(
            pystray.MenuItem("Settings...", from_tray(open_settings), default=True),
            pystray.MenuItem("Test mode (no keypresses)", from_tray(toggle_test_tray),
                             checked=lambda i: reader.test),
            pystray.MenuItem("Open config file", from_tray(open_config_file)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", from_tray(quit_app)),
        ))

    threading.Thread(target=tray.run, daemon=True).start()
    reader.log(f"{APP_NAME} started. Config: {cfg_path}")
    if load_error:
        reader.log(f"Config error, using defaults: {load_error}")
        messagebox.showerror(APP_NAME, f"Config file has an error, using defaults "
                                       f"until fixed:\n\n{load_error}")
    reader.start()
    root.after(100, pump)
    if first_run or args.settings or load_error:
        root.after(300, open_settings)
    root.mainloop()
    reader.stop_evt.set()
    reader.join(timeout=3)


def main():
    ap = argparse.ArgumentParser(description="Tap an NFC card -> do an action")
    ap.add_argument("--config", default=os.path.join(APP_DIR, "nfc_credit.toml"))
    ap.add_argument("--test", action="store_true", help="start in test mode")
    ap.add_argument("--settings", action="store_true", help="open settings on start")
    args = ap.parse_args()

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    mutex = k32.CreateMutexW(None, False, "Local\\NFC-Credit-PN532")
    if ctypes.get_last_error() == 183:     # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            None, "NFC Credit is already running - look for its icon in the system tray.",
            APP_NAME, 0x40)
        return
    try:
        run_gui(args)
    finally:
        k32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
