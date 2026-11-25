"""
NFC Credit - tap an NFC card on a PN532 (USB/CH340 serial) reader and do
something: press keys (default "5" = coin), type text, run a program, write the
card ID to a file for game logins, or ignore it. Actions can differ per card and
per program in the foreground.

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
import math
import os
import queue
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import wave
import winreg
import winsound
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
# Keyboard / mouse (SendInput with hardware scan codes - games/MAME see these)
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
KEYEVENTF_UNICODE = 0x0004
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


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.VkKeyScanW.restype = ctypes.c_short
_user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
_user32.MapVirtualKeyW.restype = wintypes.UINT
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]


def _send_input(inp):
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _send(code, up):
    if code & MOUSE:
        flags = MOUSE_FLAGS[code][1 if up else 0]
        _send_input(INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0))))
        return
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if code & EXT:
        flags |= KEYEVENTF_EXTENDEDKEY
    _send_input(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, code & 0xFF, flags, 0, 0))))


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


def type_text(text, delay=0.03):
    """Type a string. Characters on the keyboard layout are sent as real scan codes
    (with shift etc.) so games see them; anything else is sent as Unicode."""
    for ch in text:
        if ch == "\n":
            press_combo([SCANCODES["enter"]], delay)
            time.sleep(delay)
            continue
        r = _user32.VkKeyScanW(ch) if ord(ch) <= 0xFFFF else -1
        sc = _user32.MapVirtualKeyW(r & 0xFF, 0) if r != -1 else 0
        if r != -1 and sc:
            codes = []
            if r & 0x100:
                codes.append(SCANCODES["shift"])
            if r & 0x200:
                codes.append(SCANCODES["ctrl"])
            if r & 0x400:
                codes.append(SCANCODES["alt"])
            press_combo(codes + [sc], delay)
        else:
            units = ch.encode("utf-16-le")
            for unit in struct.unpack(f"<{len(units) // 2}H", units):
                for up in (False, True):
                    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
                    _send_input(INPUT(type=1, u=_INPUTUNION(
                        ki=KEYBDINPUT(0, unit, flags, 0, 0))))
        time.sleep(delay)


# --------------------------------------------------------------------------
# Foreground program
# --------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_user32.GetForegroundWindow.restype = wintypes.HWND


def foreground_exe():
    """Lower-case exe name of the program owning the foreground window, or ''."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value).lower()
    finally:
        _kernel32.CloseHandle(h)
    return ""


# --------------------------------------------------------------------------
# Sound
# --------------------------------------------------------------------------
_coin_path = None


def coin_wav():
    """Generate a short two-tone arcade coin sound once, return its path."""
    global _coin_path
    if _coin_path and os.path.exists(_coin_path):
        return _coin_path
    rate, frames = 22050, bytearray()
    for freq, dur, decay in ((988, 0.07, False), (1319, 0.40, True)):
        n = int(rate * dur)
        for i in range(n):
            env = math.exp(-5.0 * i / n) if decay else 1.0
            sq = 1.0 if math.sin(2 * math.pi * freq * i / rate) >= 0 else -1.0
            frames += struct.pack("<h", int(7000 * env * sq))
    path = os.path.join(tempfile.gettempdir(), "nfc_credit_coin.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    _coin_path = path
    return path


def resolve_path(p):
    p = os.path.expandvars(str(p).strip().strip('"'))
    return p if os.path.isabs(p) else os.path.join(APP_DIR, p)


def play_sound(spec):
    """spec: '' / 'none' = silent, 'coin' = built-in, else a .wav path."""
    if not spec or spec == "none":
        return
    try:
        path = coin_wav() if spec == "coin" else resolve_path(spec)
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
    except (RuntimeError, OSError):
        pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
ACTIONS = {  # kind: label shown in Settings
    "keys": "Press keys / mouse",
    "type": "Type text",
    "run": "Run program",
    "card_file": "Write card ID to file (game login)",
    "ignore": "Do nothing",
}
INHERIT_LABEL = "Use the global default"
CARD_FORMATS = {  # format: label
    "uid": "Card ID as read (hex)",
    "uid16": "16 hex digits (felica.txt style)",
    "digits20": "20-digit number (aime.txt access code)",
}
CARD_TYPES = {  # key: (label, hint)
    "type_a": ("Type A", "Bandai Namco Passport, older Aime/e-amusement, most NFC stickers & blank cards, amiibo"),
    "felica": ("FeliCa", "newer Aime/e-amusement (Amusement IC), Suica/PASMO, Octopus"),
    "type_b": ("Type B", "some ID cards, transit & building passes"),
    "jewel": ("Jewel/Topaz", "rare NFC Type 1 tags"),
}
DEFAULT_TYPES = ["type_a", "felica"]


def norm_uid(uid):
    return str(uid).upper().replace(":", "").replace(" ", "").replace("-", "")


def _key_list(v):
    if isinstance(v, str):
        v = [v]
    return [str(k).strip() for k in v if str(k).strip()]


def check_action(act, where):
    act = dict(act)
    kind = act.get("action", "keys")
    if kind == "keys":
        keys = _key_list(act.get("keys", ["5"]))
        if not keys:
            raise ValueError(f"{where}: no keys given")
        act["keys"] = keys
        act["_combos"] = [parse_combo(k) for k in keys]
        if int(act.get("count", 1)) < 1:
            raise ValueError(f"{where}: count must be 1 or more")
    elif kind == "type":
        if not str(act.get("text", "")):
            raise ValueError(f"{where}: 'Type text' needs some text")
    elif kind == "run":
        if not act.get("command"):
            raise ValueError(f"{where}: action 'run' needs a command")
    elif kind == "card_file":
        if not str(act.get("file", "")).strip():
            raise ValueError(f"{where}: 'Write card ID to file' needs a file path")
        if act.get("format", "uid") not in CARD_FORMATS:
            raise ValueError(f"{where}: unknown format '{act.get('format')}' "
                             f"(use {', '.join(CARD_FORMATS)})")
        act["keys"] = _key_list(act.get("keys", []))
        act["_combos"] = [parse_combo(k) for k in act["keys"]]
    elif kind != "ignore":
        raise ValueError(f"{where}: unknown action '{kind}'")
    act["action"] = kind
    return act


def parse_config(data):
    data = dict(data)
    data.setdefault("reader", {})
    data.setdefault("options", {})
    types = data["reader"].get("card_types", DEFAULT_TYPES)
    if isinstance(types, str):
        types = [types]
    bad = [t for t in types if t not in CARD_TYPES]
    if bad:
        raise ValueError(f"unknown card type '{bad[0]}' (use {', '.join(CARD_TYPES)})")
    if not types:
        raise ValueError("at least one card type must be enabled")
    data["reader"]["card_types"] = [t for t in CARD_TYPES if t in types]
    data["default"] = check_action(data.get("default", {}), "Default action")
    data["cards"] = {norm_uid(uid): check_action(act, f"Card {act.get('name') or uid}")
                     for uid, act in data.get("cards", {}).items()}
    profiles = {}
    for pname, p in data.get("profiles", {}).items():
        progs = [os.path.basename(x).lower() for x in _key_list(p.get("programs", []))]
        if not progs:
            raise ValueError(f"Profile '{pname}': list at least one program, e.g. mame.exe")
        prof = {"programs": progs,
                "cards": {norm_uid(uid): check_action(
                    act, f"Profile '{pname}', card {act.get('name') or uid}")
                    for uid, act in p.get("cards", {}).items()}}
        if p.get("default"):
            prof["default"] = check_action(p["default"], f"Profile '{pname}', default action")
        profiles[pname] = prof
    data["profiles"] = profiles
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
           "# actions: keys | type | run | card_file | ignore  (see README)",
           "",
           _table("reader", data["reader"]), "",
           _table("options", data["options"]), "",
           "# Any card that isn't listed below",
           _table("default", data["default"])]
    for uid, act in data["cards"].items():
        out += ["", _table(f'cards."{uid}"', act)]
    for pname, p in data.get("profiles", {}).items():
        q = json.dumps(pname)
        out += ["", f"# Used while one of these programs is in the foreground",
                f"[profiles.{q}]", f"programs = {_tv(p['programs'])}"]
        if p.get("default"):
            out += ["", _table(f"profiles.{q}.default", p["default"])]
        for uid, act in p.get("cards", {}).items():
            out += ["", _table(f'profiles.{q}.cards."{uid}"', act)]
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

    def profile_for(self, exe):
        for name, p in self.data["profiles"].items():
            if exe and exe in p["programs"]:
                return name, p
        return None, None

    def action_for(self, uid, exe=""):
        """-> (action, profile name or None, mapped?)
        Order: profile card > global card > profile default > global default."""
        pname, prof = self.profile_for(exe)
        if prof and uid in prof["cards"]:
            return prof["cards"][uid], pname, True
        if uid in self.data["cards"]:
            return self.data["cards"][uid], None, True
        if prof and prof.get("default"):
            return prof["default"], pname, False
        return self.data["default"], None, False


def describe(act):
    if act is None:
        return "global default"
    kind = act["action"]
    if kind == "keys":
        n = int(act.get("count", 1))
        s = f"keys {' '.join(act['keys'])}" + (f" x{n}" if n > 1 else "")
    elif kind == "type":
        s = f"type text ({len(str(act['text']))} chars{', Enter' if act.get('enter') else ''})"
    elif kind == "run":
        s = f"run {act['command']}"
    elif kind == "card_file":
        s = f"write ID to {os.path.basename(str(act['file']))} ({act.get('format', 'uid')})"
        if act.get("keys"):
            s += f", press {' '.join(act['keys'])}"
    else:
        s = "ignore"
    if act.get("sound") and kind != "ignore":
        s += " + sound" if act["sound"] != "none" else " (silent)"
    return s


def card_id_text(uid, fmt):
    if fmt == "uid16":
        return uid.rjust(16, "0")[-16:]
    if fmt == "digits20":
        return str(int(uid, 16)).rjust(20, "0")[-20:]
    return uid


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
       ("log", text) ("status", connected, text) ("tap", uid, exe)"""

    def __init__(self, cfg, test=False):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.test = test
        self.events = queue.Queue()
        self.stop_evt = threading.Event()
        self.last_uid = None
        self.last_exe = ""
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

    def do_action(self, act, uid):
        kind = act["action"]
        if kind == "ignore":
            return
        play_sound(act.get("sound") or self.cfg.data["options"].get("sound", ""))
        if self.test:
            return
        if kind == "keys":
            hold = float(act.get("hold", 0.08))
            for _ in range(int(act.get("count", 1))):
                for combo in act["_combos"]:
                    press_combo(combo, hold)
                    time.sleep(0.1)
        elif kind == "type":
            type_text(str(act["text"]))
            if act.get("enter"):
                press_combo([SCANCODES["enter"]], 0.05)
        elif kind == "run":
            try:
                cmd = act["command"]
                subprocess.Popen(cmd if isinstance(cmd, list)
                                 else shlex.split(cmd, posix=False),
                                 cwd=act.get("cwd") or None)
            except OSError as e:
                self.log(f"  run failed: {e}")
        elif kind == "card_file":
            path = resolve_path(act["file"])
            try:
                with open(path, "w", encoding="ascii") as f:
                    f.write(card_id_text(uid, act.get("format", "uid")))
            except OSError as e:
                self.log(f"  couldn't write card file: {e}")
                return
            hold = float(act.get("hold", 0.5))      # scan keys usually need a held press
            for combo in act["_combos"]:
                press_combo(combo, hold)
                time.sleep(0.1)

    def check_reload(self, port_setting):
        if not self.cfg.changed():
            return
        try:
            self.cfg.load()
            self.log(f"Config loaded: {len(self.cfg.data['cards'])} mapped card(s), "
                     f"{len(self.cfg.data['profiles'])} profile(s), "
                     f"default {describe(self.cfg.data['default'])}")
        except (ValueError, tomllib.TOMLDecodeError, OSError) as e:
            self.log(f"Config error, keeping previous settings: {e}")
            try:
                self.cfg.mtime = os.path.getmtime(self.cfg.path)
            except OSError:
                pass
        if str(self.cfg.data["reader"].get("port", "auto")) != port_setting:
            raise Reconnect()

    def handle_tap(self, uid):
        exe = foreground_exe()
        act, pname, mapped = self.cfg.action_for(uid, exe)
        name = act.get("name") or ("mapped" if mapped else "unlisted")
        self.last_uid, self.last_exe = uid, exe
        self.log(f"tap {uid} ({name}) in {exe or '?'}"
                 + (f" [profile {pname}]" if pname else "")
                 + f" -> {describe(act)}" + ("  [test mode]" if self.test else ""))
        self.events.put(("tap", uid, exe))
        self.do_action(act, uid)

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
                            self.handle_tap(uid)
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
    w = max(2, s // 14)
    cx, cy = int(s * 0.30), s // 2
    for r in (s * 0.16, s * 0.29, s * 0.42):
        d.arc((cx - r, cy - r, cx + r, cy + r), -50, 50, fill="white", width=w)
    d.ellipse((cx - w, cy - w, cx + w, cy + w), fill="white")
    return img


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
GLOBAL = "All programs (global)"


def run_gui(args):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, simpledialog
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
    state = {"win": None, "flash": None, "last_conn": None}
    GREY = "#777"

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

    def notify_status(connected, text):
        """One pop-up per connection state change."""
        prev = state["last_conn"]
        state["last_conn"] = connected
        if prev == connected or not cfg.data["options"].get("popups", True):
            return
        if connected:
            msg = f"Reader connected - {text}"
        elif prev is None:
            msg = "Reader not found - plug it in and it will connect automatically."
        else:
            msg = "Reader disconnected - searching..."

        def show(tries=10):
            try:
                if tray.visible:
                    tray.notify(msg, APP_NAME)
                    return
            except Exception:
                return
            if tries:
                root.after(500, lambda: show(tries - 1))
        show()

    # ---------------- Action editor ----------------
    class ActionEditor(ttk.Frame):
        KNOWN = {"action", "keys", "count", "command", "text", "enter", "file",
                 "format", "sound", "name"}

        def __init__(self, master):
            super().__init__(master)
            self.extra = {}
            self.allow_inherit = False
            self.kind_v = tk.StringVar(value=ACTIONS["keys"])
            self.keys_v, self.count_v = tk.StringVar(value="5"), tk.StringVar(value="1")
            self.cmd_v, self.text_v = tk.StringVar(), tk.StringVar()
            self.enter_v = tk.BooleanVar()
            self.file_v, self.after_v = tk.StringVar(), tk.StringVar(value="enter")
            self.fmt_v = tk.StringVar(value=CARD_FORMATS["uid"])
            self.sound_v = tk.StringVar()

            ttk.Label(self, text="Action", width=8).grid(row=0, column=0, sticky="w")
            self.kind_cb = ttk.Combobox(self, textvariable=self.kind_v, state="readonly",
                                        width=34, values=list(ACTIONS.values()))
            self.kind_cb.grid(row=0, column=1, sticky="w")
            self.kind_cb.bind("<<ComboboxSelected>>", lambda e: self._sync())

            f = self.frames = {}
            fk = f["keys"] = ttk.Frame(self)
            ttk.Label(fk, text="Keys", width=8).grid(row=0, column=0, sticky="w")
            ttk.Entry(fk, textvariable=self.keys_v, width=24).grid(row=0, column=1, sticky="w")
            ttk.Label(fk, text="Count").grid(row=0, column=2, padx=(12, 6))
            ttk.Spinbox(fk, from_=1, to=20, textvariable=self.count_v, width=4).grid(
                row=0, column=3, sticky="w")
            ttk.Label(fk, text="e.g. 5, shift+f1, lclick, rclick", foreground=GREY).grid(
                row=0, column=4, sticky="w", padx=(12, 0))

            ft = f["type"] = ttk.Frame(self)
            ttk.Label(ft, text="Text", width=8).grid(row=0, column=0, sticky="w")
            ttk.Entry(ft, textvariable=self.text_v, width=34).grid(row=0, column=1, sticky="we")
            ttk.Checkbutton(ft, text="Press Enter after", variable=self.enter_v).grid(
                row=0, column=2, padx=(12, 0))
            ft.columnconfigure(1, weight=1)

            fr = f["run"] = ttk.Frame(self)
            ttk.Label(fr, text="Command", width=8).grid(row=0, column=0, sticky="w")
            ttk.Entry(fr, textvariable=self.cmd_v).grid(row=0, column=1, sticky="we")
            ttk.Button(fr, text="Browse...", command=self._browse_run).grid(
                row=0, column=2, padx=(6, 0))
            fr.columnconfigure(1, weight=1)

            fc = f["card_file"] = ttk.Frame(self)
            ttk.Label(fc, text="File", width=8).grid(row=0, column=0, sticky="w")
            ttk.Entry(fc, textvariable=self.file_v).grid(row=0, column=1, columnspan=3,
                                                         sticky="we")
            ttk.Button(fc, text="Browse...", command=self._browse_file).grid(
                row=0, column=4, padx=(6, 0))
            ttk.Label(fc, text="Format", width=8).grid(row=1, column=0, sticky="w", pady=(4, 0))
            ttk.Combobox(fc, textvariable=self.fmt_v, state="readonly", width=34,
                         values=list(CARD_FORMATS.values())).grid(row=1, column=1, sticky="w",
                                                                  pady=(4, 0))
            ttk.Label(fc, text="then press").grid(row=1, column=2, padx=(12, 6), pady=(4, 0))
            ttk.Entry(fc, textvariable=self.after_v, width=10).grid(row=1, column=3,
                                                                    sticky="w", pady=(4, 0))
            fc.columnconfigure(1, weight=1)

            fs = self.sound_frame = ttk.Frame(self)
            ttk.Label(fs, text="Sound", width=8).grid(row=0, column=0, sticky="w")
            ttk.Combobox(fs, textvariable=self.sound_v, width=24,
                         values=["", "coin", "none"]).grid(row=0, column=1, sticky="w")
            ttk.Button(fs, text="Browse...", command=self._browse_sound).grid(
                row=0, column=2, padx=(6, 0))
            ttk.Label(fs, text="blank = default sound, coin = built-in, none = silent, "
                               "or a .wav", foreground=GREY).grid(row=0, column=3, sticky="w",
                                                                   padx=(12, 0))
            self.columnconfigure(1, weight=1)
            self._sync()

        def set_allow_inherit(self, flag):
            self.allow_inherit = flag
            vals = list(ACTIONS.values())
            self.kind_cb.configure(values=([INHERIT_LABEL] + vals) if flag else vals)

        def kind(self):
            label = self.kind_v.get()
            if label == INHERIT_LABEL:
                return "inherit"
            return next((k for k, v in ACTIONS.items() if v == label), "keys")

        def _browse_run(self):
            p = filedialog.askopenfilename(parent=self, title="Program to run",
                                           filetypes=[("Programs", "*.exe *.bat *.cmd *.lnk"),
                                                      ("All files", "*.*")])
            if p:
                p = os.path.normpath(p)
                self.cmd_v.set(f'"{p}"' if " " in p else p)

        def _browse_file(self):
            p = filedialog.asksaveasfilename(parent=self, title="Card ID file",
                                             confirmoverwrite=False, defaultextension=".txt",
                                             filetypes=[("Text files", "*.txt"),
                                                        ("All files", "*.*")])
            if p:
                self.file_v.set(os.path.normpath(p))

        def _browse_sound(self):
            p = filedialog.askopenfilename(parent=self, title="Sound",
                                           filetypes=[("WAV sounds", "*.wav")])
            if p:
                self.sound_v.set(os.path.normpath(p))

        def _sync(self):
            k = self.kind()
            for name, fr in self.frames.items():
                if name == k:
                    fr.grid(row=1, column=0, columnspan=2, sticky="we", pady=(6, 0))
                else:
                    fr.grid_remove()
            if k in ("ignore", "inherit"):
                self.sound_frame.grid_remove()
            else:
                self.sound_frame.grid(row=2, column=0, columnspan=2, sticky="we", pady=(6, 0))

        def set(self, act):
            if act is None:
                self.kind_v.set(INHERIT_LABEL if self.allow_inherit else ACTIONS["keys"])
                self.extra = {}
                self._sync()
                return
            self.extra = {k: v for k, v in act.items()
                          if k not in self.KNOWN and not k.startswith("_")}
            kind = act.get("action", "keys")
            self.kind_v.set(ACTIONS.get(kind, ACTIONS["keys"]))
            keys = _key_list(act.get("keys", ["5"] if kind == "keys" else []))
            self.keys_v.set(", ".join(keys) if kind == "keys" else "5")
            self.after_v.set(", ".join(keys) if kind == "card_file" else "enter")
            self.count_v.set(str(act.get("count", 1)))
            self.cmd_v.set(act.get("command", ""))
            self.text_v.set(act.get("text", ""))
            self.enter_v.set(bool(act.get("enter", False)))
            self.file_v.set(act.get("file", ""))
            self.fmt_v.set(CARD_FORMATS.get(act.get("format", "uid"), CARD_FORMATS["uid"]))
            self.sound_v.set(act.get("sound", ""))
            self._sync()

        def get(self, where):
            """-> action dict, or None for 'use the global default'. Raises ValueError."""
            k = self.kind()
            if k == "inherit":
                return None
            act = {"action": k}
            if k == "keys":
                act["keys"] = _key_list(self.keys_v.get().split(","))
                try:
                    act["count"] = int(self.count_v.get())
                except ValueError:
                    raise ValueError(f"{where}: count must be a number")
            elif k == "type":
                act["text"] = self.text_v.get()
                if self.enter_v.get():
                    act["enter"] = True
            elif k == "run":
                act["command"] = self.cmd_v.get().strip()
            elif k == "card_file":
                act["file"] = self.file_v.get().strip()
                act["format"] = next((f for f, lbl in CARD_FORMATS.items()
                                      if lbl == self.fmt_v.get()), "uid")
                act["keys"] = _key_list(self.after_v.get().split(","))
            if k != "ignore" and self.sound_v.get().strip():
                act["sound"] = self.sound_v.get().strip()
            act.update(self.extra)
            check_action(act, where)       # raises ValueError
            return act

    # ---------------- Settings window ----------------
    def open_settings():
        if state["win"] is not None:
            w = state["win"]
            w.deiconify()
            w.lift()
            w.focus_force()
            return
        data = cfg.data
        work = {GLOBAL: {"default": dict(data["default"]),
                         "cards": {u: dict(a) for u, a in data["cards"].items()}}}
        for pname, p in data["profiles"].items():
            work[pname] = {"programs": list(p["programs"]),
                           "default": dict(p["default"]) if p.get("default") else None,
                           "cards": {u: dict(a) for u, a in p["cards"].items()}}
        cur = [GLOBAL]

        w = tk.Toplevel(root)
        state["win"] = w
        w.title(f"{APP_NAME} - Settings")
        w.minsize(760, 730)     # tall enough for every action type, so it doesn't jump
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        tab_cards, tab_reader, tab_opts, tab_log = (ttk.Frame(nb) for _ in range(4))
        nb.add(tab_cards, text="Cards")
        nb.add(tab_reader, text="Reader")
        nb.add(tab_opts, text="Options")
        nb.add(tab_log, text="Activity")
        pad = {"padx": 8, "pady": 6}

        # ===== Cards tab =====
        fp = ttk.LabelFrame(tab_cards, text="Program profile")
        fp.pack(fill="x", **pad)
        prof_v = tk.StringVar(value=GLOBAL)
        ttk.Label(fp, text="Profile", width=9).grid(row=0, column=0, sticky="w", padx=8, pady=6)
        prof_cb = ttk.Combobox(fp, textvariable=prof_v, state="readonly", width=28)
        prof_cb.grid(row=0, column=1, sticky="w")
        ttk.Button(fp, text="New profile...", command=lambda: new_profile()).grid(
            row=0, column=2, padx=(8, 0))
        del_btn = ttk.Button(fp, text="Delete profile", command=lambda: delete_profile())
        del_btn.grid(row=0, column=3, padx=(6, 0))
        prof_hint = ttk.Label(fp, foreground=GREY, wraplength=680, justify="left")
        prof_hint.grid(row=2, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 6))
        progs_row = ttk.Frame(fp)
        progs_v = tk.StringVar()
        ttk.Label(progs_row, text="Programs", width=9).grid(row=0, column=0, sticky="w")
        ttk.Entry(progs_row, textvariable=progs_v, width=40).grid(row=0, column=1, sticky="we")

        def use_last_program():
            if not reader.last_exe:
                messagebox.showinfo(APP_NAME, "Tap a card while the program is in front "
                                              "first.", parent=w)
                return
            progs = _key_list(progs_v.get().split(","))
            if reader.last_exe not in progs:
                progs.append(reader.last_exe)
            progs_v.set(", ".join(progs))

        ttk.Button(progs_row, text="Use last tapped program", command=use_last_program).grid(
            row=0, column=2, padx=(6, 0))
        progs_row.columnconfigure(1, weight=1)

        fd = ttk.LabelFrame(tab_cards, text="Default action")
        fd.pack(fill="x", **pad)
        def_ed = ActionEditor(fd)
        def_ed.pack(fill="x", padx=8, pady=6)

        fc = ttk.LabelFrame(tab_cards, text="Mapped cards")
        fc.pack(fill="both", expand=True, **pad)
        tree = ttk.Treeview(fc, columns=("uid", "name", "does"), show="headings", height=5)
        for col, txt, wd in (("uid", "Card ID", 150), ("name", "Name", 170),
                             ("does", "Does", 320)):
            tree.heading(col, text=txt)
            tree.column(col, width=wd, anchor="w")
        tree.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        ed = ttk.Frame(fc)
        ed.pack(fill="x", padx=8, pady=6)
        uid_v, name_v = tk.StringVar(), tk.StringVar()
        ttk.Label(ed, text="Card ID", width=8).grid(row=0, column=0, sticky="w")
        ttk.Entry(ed, textvariable=uid_v, width=20).grid(row=0, column=1, sticky="w")

        def cards():
            return work[cur[0]]["cards"]

        def use_last():
            if reader.last_uid:
                uid_v.set(reader.last_uid)
                if reader.last_uid in cards():
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
            for uid, a in cards().items():
                try:
                    does = describe(check_action(a, uid))
                except ValueError:
                    does = "?"
                tree.insert("", "end", iid=uid, values=(uid, a.get("name", ""), does))

        def on_select(_e=None):
            sel = tree.selection()
            if sel:
                a = cards()[sel[0]]
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
            cards()[uid] = act
            fill_tree()
            tree.selection_set(uid)
            return True

        def remove():
            for uid in tree.selection():
                cards().pop(uid, None)
            fill_tree()
            uid_v.set("")
            name_v.set("")

        def clear():
            tree.selection_remove(*tree.selection())
            uid_v.set("")
            name_v.set("")
            card_ed.set({"action": "keys", "keys": ["5"], "count": 1})

        bf = ttk.Frame(fc)
        bf.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(bf, text="Add / Update card", command=add_update).pack(side="left")
        ttk.Button(bf, text="Remove", command=remove).pack(side="left", padx=6)
        ttk.Button(bf, text="Clear", command=clear).pack(side="left")

        def stash():
            """Pull the editors into work[cur]. False (after showing why) if invalid."""
            if uid_v.get().strip() and not add_update():
                return False
            p = work[cur[0]]
            try:
                d = def_ed.get("Default action")
            except ValueError as e:
                messagebox.showerror(APP_NAME, str(e), parent=w)
                return False
            p["default"] = d
            if cur[0] != GLOBAL:
                p["programs"] = [os.path.basename(x).lower()
                                 for x in _key_list(progs_v.get().split(","))]
            return True

        def load_profile(name):
            cur[0] = name
            prof_cb.configure(values=[GLOBAL] + [n for n in work if n != GLOBAL])
            prof_v.set(name)
            p = work[name]
            is_global = name == GLOBAL
            def_ed.set_allow_inherit(not is_global)
            def_ed.set(p["default"])
            fd.configure(text="Default action (any card not listed below)" if is_global
                         else "Default action in this profile (cards not mapped here "
                              "or globally)")
            fc.configure(text="Mapped cards" if is_global
                         else f"Mapped cards in profile '{name}' (override global ones)")
            if is_global:
                progs_row.grid_remove()
                del_btn.state(["disabled"])
                prof_hint.configure(text="Global settings apply in every program. Add a "
                                         "profile to make cards do something different "
                                         "while a specific program is in front.")
            else:
                progs_row.grid(row=1, column=0, columnspan=6, sticky="we", padx=8,
                               pady=(0, 4))
                progs_v.set(", ".join(p.get("programs", [])))
                del_btn.state(["!disabled"])
                prof_hint.configure(text="Active while one of these programs (exe names, "
                                         "comma separated) is in the foreground. Tap a card "
                                         "with the game in front, then use 'Use last tapped "
                                         "program'.")
            fill_tree()
            clear()

        def on_profile_change(_e=None):
            new = prof_v.get()
            if new == cur[0]:
                return
            if not stash():
                prof_v.set(cur[0])
                return
            load_profile(new)

        prof_cb.bind("<<ComboboxSelected>>", on_profile_change)

        def new_profile():
            name = simpledialog.askstring(APP_NAME, "Profile name (e.g. MAME):", parent=w)
            if name is None:
                return
            name = name.strip()
            if not name or name in work:
                messagebox.showerror(APP_NAME, "Enter a new, unique profile name.", parent=w)
                return
            if not stash():
                return
            work[name] = {"programs": [], "default": None, "cards": {}}
            load_profile(name)

        def delete_profile():
            if cur[0] == GLOBAL:
                return
            if messagebox.askyesno(APP_NAME, f"Delete profile '{cur[0]}'?", parent=w):
                del work[cur[0]]
                load_profile(GLOBAL)

        # ===== Reader tab =====
        fr = ttk.LabelFrame(tab_reader, text="Reader")
        fr.pack(fill="x", **pad)
        port_v = tk.StringVar(value=str(data["reader"].get("port", "auto")))
        ports = ["auto"] + [p.device for p in serial.tools.list_ports.comports()]
        ttk.Label(fr, text="Port").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(fr, textvariable=port_v, values=ports, width=10).grid(
            row=0, column=1, sticky="w")
        ttk.Label(fr, text="auto finds the reader on any COM port", foreground=GREY).grid(
            row=0, column=2, columnspan=3, sticky="w", padx=8)
        rt_v = tk.StringVar(value=str(data["reader"].get("remove_time", 0.6)))
        ttk.Label(fr, text="Re-tap delay (s)").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Spinbox(fr, from_=0.1, to=10, increment=0.1, textvariable=rt_v,
                    width=6).grid(row=1, column=1, sticky="w")
        ttk.Label(fr, text="how long a card must be off the reader before it can tap again",
                  foreground=GREY).grid(row=1, column=2, columnspan=3, sticky="w", padx=8)
        pi_v = tk.StringVar(value=str(data["reader"].get("poll_interval", 0.05)))
        ttk.Label(fr, text="Poll every (s)").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Spinbox(fr, from_=0.02, to=2, increment=0.05, textvariable=pi_v,
                    width=6).grid(row=2, column=1, sticky="w")
        ttk.Label(fr, text="0.05 = most responsive, 0.2 = less reader traffic",
                  foreground=GREY).grid(row=2, column=2, columnspan=3, sticky="w", padx=8)

        ft = ttk.LabelFrame(tab_reader, text="Card types")
        ft.pack(fill="x", **pad)
        type_vars = {}
        for i, (key, (label, hint)) in enumerate(CARD_TYPES.items()):
            v = tk.BooleanVar(value=key in data["reader"]["card_types"])
            type_vars[key] = v
            ttk.Checkbutton(ft, text=label, variable=v).grid(row=i, column=0, sticky="w",
                                                             padx=8, pady=2)
            ttk.Label(ft, text=hint, foreground=GREY).grid(row=i, column=1, sticky="w",
                                                           padx=(8, 0))

        # ===== Options tab =====
        fo = ttk.LabelFrame(tab_opts, text="General")
        fo.pack(fill="x", **pad)
        startup_v = tk.BooleanVar(value=get_startup())
        test_v = tk.BooleanVar(value=reader.test)
        popups_v = tk.BooleanVar(value=bool(data["options"].get("popups", True)))
        log_v = tk.StringVar(value=data["options"].get("log_file", ""))
        ttk.Checkbutton(fo, text="Start with Windows", variable=startup_v).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=4)

        def toggle_test():
            reader.test = test_v.get()
            reader.log(f"Test mode {'ON - taps are logged, no actions' if reader.test else 'off'}")
            refresh_icon()
            tray.update_menu()

        ttk.Checkbutton(fo, text="Test mode (log taps, don't press keys or run anything)",
                        variable=test_v, command=toggle_test).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(fo, text="Show a pop-up when the reader connects or disconnects",
                        variable=popups_v).grid(row=2, column=0, columnspan=3, sticky="w",
                                                padx=8, pady=4)
        ttk.Label(fo, text="Log file").grid(row=3, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(fo, textvariable=log_v, width=24).grid(row=3, column=1, sticky="w")
        ttk.Label(fo, text="optional, e.g. nfc_credit.log (next to the exe)",
                  foreground=GREY).grid(row=3, column=2, sticky="w", padx=8)

        fsnd = ttk.LabelFrame(tab_opts, text="Sound on tap")
        fsnd.pack(fill="x", **pad)
        snd_v = tk.StringVar(value=data["options"].get("sound", ""))
        ttk.Label(fsnd, text="Default sound").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(fsnd, textvariable=snd_v, values=["", "coin"], width=30).grid(
            row=0, column=1, sticky="w")

        def browse_snd():
            p = filedialog.askopenfilename(parent=w, title="Sound",
                                           filetypes=[("WAV sounds", "*.wav")])
            if p:
                snd_v.set(os.path.normpath(p))

        ttk.Button(fsnd, text="Browse...", command=browse_snd).grid(row=0, column=2, padx=6)
        ttk.Button(fsnd, text="Test", command=lambda: play_sound(snd_v.get().strip())).grid(
            row=0, column=3)
        ttk.Label(fsnd, text="blank = no sound, coin = built-in arcade coin, or pick a .wav. "
                             "Cards can override this.", foreground=GREY).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))

        # ===== Activity tab =====
        log_t = tk.Text(tab_log, height=10, wrap="none", font=("Consolas", 9))
        log_t.pack(fill="both", expand=True, padx=8, pady=8)
        log_t.insert("end", "\n".join(reader.log_lines) + "\n")
        log_t.see("end")
        log_t.configure(state="disabled")

        # ===== Bottom bar =====
        fb = ttk.Frame(w)
        fb.pack(fill="x", padx=10, pady=(0, 10))
        status_l = ttk.Label(fb, text="")
        status_l.pack(side="top", anchor="w", pady=(0, 4))
        saved_l = ttk.Label(fb, text="", foreground=GREY)
        saved_l.pack(side="left")

        def close():
            state["win"] = None
            w.destroy()

        def save():
            if not stash():
                nb.select(tab_cards)
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
            profiles = {}
            for name, p in work.items():
                if name == GLOBAL:
                    continue
                profiles[name] = {"programs": p["programs"], "cards": p["cards"]}
                if p["default"]:
                    profiles[name]["default"] = p["default"]
            opts = {**cfg.data["options"], "log_file": log_v.get().strip(),
                    "popups": popups_v.get()}
            if snd_v.get().strip():
                opts["sound"] = snd_v.get().strip()
            else:
                opts.pop("sound", None)
            try:
                cfg.save({
                    "reader": {**cfg.data["reader"], "port": port_v.get().strip() or "auto",
                               "remove_time": rt, "poll_interval": pi, "card_types": types},
                    "options": opts,
                    "default": work[GLOBAL]["default"],
                    "cards": work[GLOBAL]["cards"],
                    "profiles": profiles,
                })
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
            n_cards = sum(len(p["cards"]) for p in work.values())
            summary = (f"{n_cards} mapped card(s), {len(profiles)} profile(s), "
                       f"default: {describe(cfg.data['default'])}")
            reader.log(f"Settings saved: {summary}")
            saved_l.configure(text=f"Saved {time.strftime('%H:%M:%S')} - {summary}")

        ttk.Button(fb, text="Close", command=close).pack(side="right")
        ttk.Button(fb, text="Save", command=save).pack(side="right", padx=6)
        w.protocol("WM_DELETE_WINDOW", close)

        def status_update():
            s = ("● " if reader.connected else "○ ") + reader.status_text
            if reader.last_uid:
                s += f"     Last tap: {reader.last_uid} in {reader.last_exe or '?'}"
            status_l.configure(text=s)

        w._status_update = status_update
        w._log_widget = log_t
        load_profile(GLOBAL)
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
                notify_status(ev[1], ev[2])
                if w is not None:
                    w._status_update()
            elif ev[0] == "tap":
                refresh_icon("tap")
                if state["flash"]:
                    root.after_cancel(state["flash"])
                state["flash"] = root.after(500, refresh_icon)
                if w is not None:
                    w._status_update()
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

    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex = _kernel32.CreateMutexW(None, False, "Local\\NFC-Credit-PN532")
    if ctypes.get_last_error() == 183:     # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            None, "NFC Credit is already running - look for its icon in the system tray.",
            APP_NAME, 0x40)
        return
    try:
        run_gui(args)
    finally:
        _kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
