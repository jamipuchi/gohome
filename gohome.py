#!/usr/bin/env python3
"""gohome: when the MacBook lid closes, ask on Telegram whether you're going home.

Going home  -> stay awake (screen locked) and join the iPhone Personal Hotspot.
Not going / no answer in time -> sleep as usual.

macOS sleeps the instant the lid closes and nothing can delay it, so this helper
inverts the default: it keeps `pmset -a disablesleep 1` set while it runs, watches
the lid itself, and puts the Mac to sleep when that is the right answer. Whenever
it exits it sets `disablesleep 0` again.

Python 3 standard library only. Runs as a root LaunchDaemon (pmset needs root).
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

__version__ = "1.0.0"

DEFAULT_CONFIG_PATH = "/usr/local/etc/gohome.json"
DEFAULT_LOG_PATH = "/var/log/gohome.log"

POLL_INTERVAL = 2.0           # seconds between lid checks
SEND_RETRY_SECONDS = 15       # retry sending the question this often while Telegram is unreachable
SLEEP_RETRY_SECONDS = 60      # re-issue `pmset sleepnow` if no sleep was detected after this long
RESLEEP_AFTER_WAKE_SECONDS = 300  # lid still closed and awake this long after a wake: sleep again
DISPLAY_CHECK_SECONDS = 10    # how often to re-check for an external display while docked
BATTERY_CHECK_SECONDS = 60
SLEEP_GAP_THRESHOLD = 5.0     # a jump this large between the two clocks means the Mac slept

ANSWER_HOME = "home"
ANSWER_NOT_HOME = "not_home"
QUESTION_TEXT = "💻 Lid closed. Going straight home?"
QUESTION_BUTTONS = [[
    {"text": "🏠 Going home", "callback_data": ANSWER_HOME},
    {"text": "🚫 Not going home", "callback_data": ANSWER_NOT_HOME},
]]

IDLE = "IDLE"
ASKING = "ASKING"
HOME = "HOME"
DOCKED = "DOCKED"
SLEEPING = "SLEEPING"

DEFAULTS = {
    "wifi_interface": "",             # empty: detect with `networksetup -listallhardwareports`
    "hotspot_password": "",           # empty: rely on the password macOS already remembers
    "ask_timeout_seconds": 180,
    "max_awake_minutes": 90,
    "hotspot_retry_seconds": 10,
    "hotspot_give_up_seconds": 300,
    "hotspot_subnet_prefix": "172.20.10.",  # iPhone hotspots hand out 172.20.10.x
    "min_battery_percent": 10,
    "skip_when_external_display": True,
    "telegram_api_base": "https://api.telegram.org",  # only changed by the tests
}
REQUIRED = ("telegram_bot_token", "telegram_chat_id", "hotspot_ssid")


class ConfigError(Exception):
    pass


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    missing = [k for k in REQUIRED if not str(raw.get(k, "")).strip()]
    if missing:
        raise ConfigError(f"missing required setting(s): {', '.join(missing)}")
    unknown = sorted(set(raw) - set(DEFAULTS) - set(REQUIRED))
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(unknown)}")
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    for key in ("ask_timeout_seconds", "max_awake_minutes", "hotspot_retry_seconds",
                "hotspot_give_up_seconds", "min_battery_percent"):
        if not isinstance(cfg[key], (int, float)) or isinstance(cfg[key], bool) or cfg[key] < 0:
            raise ConfigError(f"{key} must be a non-negative number")
    if not re.fullmatch(r"-?\d+", str(cfg["telegram_chat_id"]).strip()):
        raise ConfigError("telegram_chat_id must be a number")
    cfg["telegram_chat_id"] = int(str(cfg["telegram_chat_id"]).strip())
    return cfg


# ---------------------------------------------------------------- logging

class RedactSecrets(logging.Filter):
    """Masks the bot token and hotspot password in every log record."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 4]

    def filter(self, record):
        msg = record.getMessage()
        for s in self.secrets:
            msg = msg.replace(s, "********")
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            for s in self.secrets:
                record.exc_text = record.exc_text.replace(s, "********")
        record.msg, record.args = msg, None
        return True


def setup_logging(log_file: Optional[str], secrets) -> logging.Logger:
    log = logging.getLogger("gohome")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    handlers = []
    if log_file:
        try:
            handlers.append(logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
        except OSError as e:
            print(f"gohome: cannot write {log_file} ({e}); logging to stderr only", file=sys.stderr)
    if not handlers or sys.stderr.isatty():
        handlers.append(logging.StreamHandler(sys.stderr))
    redact = RedactSecrets(secrets)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(redact)
        log.addHandler(h)
    return log


# ---------------------------------------------------------------- clock and lid

class Clock:
    """now() keeps counting while the Mac sleeps; awake() does not. Their difference grows by
    exactly the time spent asleep, which is how the helper notices a sleep/wake cycle."""

    def now(self) -> float:
        return time.clock_gettime(time.CLOCK_MONOTONIC)

    def awake(self) -> float:
        return time.clock_gettime(time.CLOCK_UPTIME_RAW)


def parse_clamshell(ioreg_output: str) -> Optional[bool]:
    m = re.search(r'"AppleClamshellState"\s*=\s*(Yes|No)', ioreg_output)
    return None if m is None else m.group(1) == "Yes"


class IORegLid:
    def __init__(self, log):
        self.log = log

    def is_closed(self) -> Optional[bool]:
        try:
            out = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as e:
            self.log.warning("Cannot read lid state: %s", e)
            return None
        return parse_clamshell(out)


class SimulatedLid:
    """--simulate-lid: the lid state is whatever you type on stdin (open / closed)."""

    def __init__(self, closed: bool, log):
        self.closed = closed
        self.log = log

    def is_closed(self) -> Optional[bool]:
        return self.closed

    def read_stdin(self):
        self.log.info("Simulated lid is %s. Type 'open' or 'closed' (o/c) and press Enter.",
                      "closed" if self.closed else "open")
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd in ("o", "open"):
                self.closed = False
            elif cmd in ("c", "close", "closed"):
                self.closed = True
            elif cmd:
                self.log.info("Unknown command %r; use 'open' or 'closed'", cmd)
                continue
            else:
                continue
            self.log.info("Simulated lid set to %s", "closed" if self.closed else "open")


# ---------------------------------------------------------------- macOS actions

def detect_wifi_interface() -> Optional[str]:
    try:
        out = subprocess.run(["networksetup", "-listallhardwareports"],
                             capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", out)
    return m.group(1) if m else None


def parse_battery(pmset_output: str):
    """Returns (percent, on_ac_power) from `pmset -g batt`, or (None, True) without a battery."""
    m = re.search(r"(\d+)%", pmset_output)
    on_ac = "AC Power" in pmset_output
    return (int(m.group(1)) if m else None), on_ac


class MacSystem:
    """Everything that touches the Mac. With dry_run=True, commands that change something are
    logged instead of run; read-only queries (battery, displays, IP address) still run."""

    def __init__(self, cfg: dict, log, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run
        self.iface = cfg["wifi_interface"] or detect_wifi_interface() or "en0"
        self._caffeinate: Optional[subprocess.Popen] = None

    def _run(self, cmd, shown=None, timeout=30) -> Optional[subprocess.CompletedProcess]:
        shown = shown or cmd
        if self.dry_run:
            self.log.info("[dry-run] would run: %s", " ".join(shown))
            return None
        self.log.info("Running: %s", " ".join(shown))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            self.log.error("%s failed: %s", shown[0], e)
            return None
        if res.returncode != 0:
            self.log.error("%s exited %d: %s", shown[0], res.returncode,
                           (res.stdout + res.stderr).strip())
        return res

    def lock_screen(self):
        self._run(["pmset", "displaysleepnow"])

    def set_sleep_disabled(self, disabled: bool):
        self._run(["pmset", "-a", "disablesleep", "1" if disabled else "0"])

    def sleep_now(self):
        self._run(["pmset", "sleepnow"])

    def start_keepawake(self):
        if self._caffeinate and self._caffeinate.poll() is None:
            return
        # -w: caffeinate exits by itself if this helper dies, even from SIGKILL
        cmd = ["caffeinate", "-i", "-w", str(os.getpid())]
        if self.dry_run:
            self.log.info("[dry-run] would run: %s", " ".join(cmd))
            return
        self.log.info("Starting: %s", " ".join(cmd))
        try:
            self._caffeinate = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
        except OSError as e:
            self.log.error("caffeinate failed: %s", e)

    def stop_keepawake(self):
        if self._caffeinate is None:
            return
        if self._caffeinate.poll() is None:
            self.log.info("Stopping caffeinate")
            self._caffeinate.terminate()
            try:
                self._caffeinate.wait(5)
            except subprocess.TimeoutExpired:
                self._caffeinate.kill()
        self._caffeinate = None

    def _ip_address(self) -> str:
        try:
            return subprocess.run(["ipconfig", "getifaddr", self.iface], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def _on_hotspot(self) -> bool:
        prefix = self.cfg["hotspot_subnet_prefix"]
        return bool(prefix) and self._ip_address().startswith(prefix)

    def join_hotspot(self) -> bool:
        ssid, password = self.cfg["hotspot_ssid"], self.cfg["hotspot_password"]
        cmd = ["networksetup", "-setairportnetwork", self.iface, ssid]
        shown = cmd + (["********"] if password else [])
        if password:
            cmd.append(password)
        if self.dry_run:
            self.log.info("[dry-run] would run: %s", " ".join(shown))
            return True
        if self._on_hotspot():
            self.log.info("Already on the hotspot (%s)", self._ip_address())
            return True
        res = self._run(cmd, shown=shown, timeout=60)
        if res is None:
            return False
        output = (res.stdout + res.stderr).strip()
        # networksetup often exits 0 even when it could not join, so read what it printed
        if res.returncode != 0 or re.search(r"error|could not|failed|unable", output, re.I):
            self.log.info("Join failed: %s", output or f"exit {res.returncode}")
            return False
        if not self.cfg["hotspot_subnet_prefix"]:
            return True
        for _ in range(10):
            if self._on_hotspot():
                return True
            time.sleep(1.5)
        self.log.info("networksetup reported no error but %s has address %r, not on %s*",
                      self.iface, self._ip_address(), self.cfg["hotspot_subnet_prefix"])
        return False

    def battery(self):
        try:
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None, True
        return parse_battery(out)

    def external_display_connected(self) -> bool:
        try:
            out = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"],
                                 capture_output=True, text=True, timeout=20).stdout
            data = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self.log.warning("Cannot list displays: %s", e)
            return False
        for gpu in data.get("SPDisplaysDataType", []):
            for d in gpu.get("spdisplays_ndrvs", []):
                if d.get("spdisplays_connection_type") != "spdisplays_internal":
                    return True
        return False


# ---------------------------------------------------------------- Telegram

class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, chat_id: int, base: str = "https://api.telegram.org",
                 timeout: float = 10):
        self.token = token
        self.chat_id = chat_id
        self.base = base.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None):
        req = urllib.request.Request(
            f"{self.base}/bot{self.token}/{method}",
            data=json.dumps(params or {}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except ValueError:
                raise TelegramError(f"{method}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            # never include the URL: it contains the bot token
            raise TelegramError(f"{method}: network error: {getattr(e, 'reason', e)}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'unknown error')}")
        return body.get("result")

    def send_question(self, text: str, buttons) -> int:
        result = self.call("sendMessage", {"chat_id": self.chat_id, "text": text,
                                           "reply_markup": {"inline_keyboard": buttons}})
        return result["message_id"]

    def edit_message(self, message_id: int, text: str):
        # editing without reply_markup also removes the buttons
        try:
            self.call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id,
                                          "text": text})
        except TelegramError as e:
            if "message is not modified" not in str(e):
                raise

    def answer_callback(self, callback_id: str, text: str):
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, offset: Optional[int], timeout: int):
        params = {"timeout": timeout, "allowed_updates": ["callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 10)


class TelegramAsker:
    """What the state machine talks to. ask() sends the question; a background thread
    long-polls getUpdates and records the answer, which answer() reads without blocking."""

    POLL_TIMEOUT = 25

    def __init__(self, api: TelegramAPI, log):
        self.api = api
        self.log = log
        self._lock = threading.Lock()
        self._active: Optional[int] = None
        self._answers: dict = {}
        self._offset: Optional[int] = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def ask(self) -> Optional[int]:
        try:
            msg_id = self.api.send_question(QUESTION_TEXT, QUESTION_BUTTONS)
        except TelegramError as e:
            self.log.warning("Could not send the question: %s", e)
            return None
        with self._lock:
            self._active = msg_id
            self._answers.clear()
        self._wake.set()
        self.log.info("Question sent (message %s)", msg_id)
        return msg_id

    def answer(self, msg_id: int) -> Optional[str]:
        with self._lock:
            return self._answers.get(msg_id)

    def finish(self, msg_id: int, text: str):
        """Stop accepting answers for msg_id and replace the question with text."""
        with self._lock:
            if self._active == msg_id:
                self._active = None
        self.edit(msg_id, text)

    def edit(self, msg_id: int, text: str):
        try:
            self.api.edit_message(msg_id, text)
        except TelegramError as e:
            self.log.warning("Could not edit message %s: %s", msg_id, e)

    def _run(self):
        try:  # skip button presses left over from before this helper started
            old = self.api.get_updates(-1, 0)
            if old:
                self._offset = old[-1]["update_id"] + 1
        except TelegramError as e:
            self.log.warning("Could not skip old Telegram updates: %s", e)
        while not self._stop.is_set():
            if self._active is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                updates = self.api.get_updates(self._offset, self.POLL_TIMEOUT)
            except TelegramError as e:
                self.log.warning("getUpdates failed: %s", e)
                self._stop.wait(5)
                continue
            for update in updates:
                self._offset = update["update_id"] + 1
                self.handle_update(update)

    def handle_update(self, update: dict):
        cq = update.get("callback_query")
        if not cq:
            return
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if str(chat_id) != str(self.api.chat_id):
            self.log.info("Ignoring a button press from another chat")
            return
        data = cq.get("data")
        with self._lock:
            active = self._active
            accepted = active is not None and msg.get("message_id") == active and \
                data in (ANSWER_HOME, ANSWER_NOT_HOME)
            if accepted:
                self._answers[active] = data
        try:
            self.api.answer_callback(cq.get("id", ""), "Got it" if accepted else "This question has expired")
        except TelegramError as e:
            self.log.warning("answerCallbackQuery failed: %s", e)
        if accepted:
            self.log.info("Answer received: %s", data)
        else:
            self.log.info("Ignoring button %r on message %s (waiting for %s)",
                          data, msg.get("message_id"), active)


# ---------------------------------------------------------------- state machine

class GoHome:
    def __init__(self, cfg: dict, lid, system, messenger, clock, log):
        self.cfg = cfg
        self.lid = lid
        self.system = system
        self.messenger = messenger
        self.clock = clock
        self.log = log
        self.state: Optional[str] = None
        self.msg_id: Optional[int] = None
        self._lid_closed = False
        self._clock_offset = clock.now() - clock.awake()

    def _set_state(self, new: str, why: str):
        self.log.info("State %s -> %s (%s)", self.state or "START", new, why)
        self.state = new

    def start(self):
        self.system.set_sleep_disabled(True)
        self._set_state(IDLE, "helper started")
        # if the lid is already closed, the first tick() treats it as a fresh close

    def shutdown(self):
        # restore normal sleep first so the Mac can never be left unable to sleep
        self.system.set_sleep_disabled(False)
        self.system.stop_keepawake()
        if self.state == ASKING and self.msg_id is not None:
            self.messenger.finish(self.msg_id, "⏹ Helper stopped")
        self.log.info("Stopped; disablesleep set back to 0")

    def tick(self):
        now = self.clock.now()
        closed = self.lid.is_closed()
        if closed is None:
            closed = self._lid_closed
        if closed != self._lid_closed:
            self.log.info("Lid %s", "closed" if closed else "opened")
        self._lid_closed = closed
        slept = self._detect_sleep()
        handler = {IDLE: self._tick_idle, ASKING: self._tick_asking, HOME: self._tick_home,
                   DOCKED: self._tick_docked, SLEEPING: self._tick_sleeping}[self.state]
        handler(closed, now, slept)

    def _detect_sleep(self) -> float:
        offset = self.clock.now() - self.clock.awake()
        gap, self._clock_offset = offset - self._clock_offset, offset
        if gap > SLEEP_GAP_THRESHOLD:
            self.log.info("Woke up after sleeping %.0f s", gap)
            return gap
        return 0.0

    # IDLE: lid open, disablesleep 1, waiting for the lid to close
    def _tick_idle(self, closed, now, slept):
        if closed:
            self._lid_just_closed(now)

    def _lid_just_closed(self, now):
        if self.cfg["skip_when_external_display"] and self.system.external_display_connected():
            self._set_state(DOCKED, "external display connected, not asking")
            self._next_display_check = now + DISPLAY_CHECK_SECONDS
            return
        self._start_asking(now)

    # DOCKED: lid closed on an external display (clamshell mode); stay out of the way
    def _tick_docked(self, closed, now, slept):
        if not closed:
            self._set_state(IDLE, "lid opened")
        elif now >= self._next_display_check:
            self._next_display_check = now + DISPLAY_CHECK_SECONDS
            if not self.system.external_display_connected():
                self.log.info("External display disconnected while the lid is closed")
                self._start_asking(now)

    # ASKING: screen locked, question sent, waiting for a button
    def _start_asking(self, now):
        self._set_state(ASKING, "lid closed")
        self.system.lock_screen()
        self._ask_started = now
        self.msg_id = None
        self._send_question(now)

    def _send_question(self, now):
        self.msg_id = self.messenger.ask()
        if self.msg_id is None:
            self._next_send = now + SEND_RETRY_SECONDS

    def _tick_asking(self, closed, now, slept):
        if not closed:
            self._finish_message("↩️ Cancelled (lid opened)")
            self._set_state(IDLE, "lid opened while asking")
            return
        timeout = self.cfg["ask_timeout_seconds"]
        if self.msg_id is None and now >= self._next_send and now - self._ask_started < timeout:
            self._send_question(now)
        answer = self.messenger.answer(self.msg_id) if self.msg_id is not None else None
        if answer == ANSWER_HOME:
            self._start_home(now)
        elif answer == ANSWER_NOT_HOME:
            self._start_sleeping(now, "not going home")
        elif now - self._ask_started >= timeout:
            if self.msg_id is None:
                self._start_sleeping(now, "Telegram unreachable")
            else:
                self._start_sleeping(now, f"no answer within {timeout:g} s")

    def _finish_message(self, text):
        if self.msg_id is not None:
            self.messenger.finish(self.msg_id, text)

    # HOME: stay awake and get on the hotspot
    def _start_home(self, now):
        self._set_state(HOME, "going home")
        self._finish_message("🏠 Going home: staying awake, joining hotspot…")
        self.system.start_keepawake()
        self._home_started = now
        self._hotspot_pending = True
        self._hotspot_attempts = 0
        self._next_hotspot_attempt = now
        self._hotspot_deadline = now + self.cfg["hotspot_give_up_seconds"]
        self._next_battery_check = now

    def _tick_home(self, closed, now, slept):
        if not closed:
            self.system.stop_keepawake()
            self._set_state(IDLE, "lid opened")
            return
        max_awake = self.cfg["max_awake_minutes"]
        if now - self._home_started >= max_awake * 60:
            self._start_sleeping(now, f"awake for {max_awake:g} min, the maximum")
            return
        if now >= self._next_battery_check:
            self._next_battery_check = now + BATTERY_CHECK_SECONDS
            percent, on_ac = self.system.battery()
            if percent is not None and not on_ac and percent <= self.cfg["min_battery_percent"]:
                self._start_sleeping(now, f"battery at {percent}%")
                return
        if self._hotspot_pending and now >= self._next_hotspot_attempt:
            self._hotspot_attempts += 1
            self.log.info("Hotspot join attempt %d", self._hotspot_attempts)
            if self.system.join_hotspot():
                self._hotspot_pending = False
                self.log.info("Connected to hotspot")
                self.messenger.edit(self.msg_id, "🏠 Going home: ✅ Connected to hotspot")
            elif self.clock.now() >= self._hotspot_deadline:
                self._hotspot_pending = False
                self.log.info("Hotspot not found after %d attempts; giving up",
                              self._hotspot_attempts)
                self.messenger.edit(
                    self.msg_id, f"🏠 Going home: ⚠️ Hotspot not found "
                                 f"({self._hotspot_attempts} attempts). Staying awake anyway.")
            else:
                self._next_hotspot_attempt = self.clock.now() + self.cfg["hotspot_retry_seconds"]

    # SLEEPING: disablesleep 0 and sleepnow; re-arm only once the lid opens again
    def _start_sleeping(self, now, reason):
        self.system.stop_keepawake()
        self._set_state(SLEEPING, reason)
        self._finish_message(f"😴 Sleeping ({reason})")
        self.system.set_sleep_disabled(False)
        self.system.sleep_now()
        self._last_sleep_event = now
        self._resleep_after = SLEEP_RETRY_SECONDS

    def _tick_sleeping(self, closed, now, slept):
        if slept:
            # macOS may wake with the lid still closed (Power Nap, network wake). Sleep is
            # allowed again (disablesleep 0), so it goes back to sleep by itself, and we don't
            # ask again: that only happens after the lid has been opened.
            self._last_sleep_event = now
            self._resleep_after = RESLEEP_AFTER_WAKE_SECONDS
        if not closed:
            self.system.set_sleep_disabled(True)
            self._set_state(IDLE, "lid opened")
        elif now - self._last_sleep_event >= self._resleep_after:
            self.log.warning("Lid closed but still awake after %.0f s; sleeping again",
                             now - self._last_sleep_event)
            self.system.set_sleep_disabled(False)
            self.system.sleep_now()
            self._last_sleep_event = now


# ---------------------------------------------------------------- entry point

def run_check(cfg: dict, config_path: str, log) -> int:
    ok = True
    try:
        st = os.stat(config_path)
        if st.st_mode & 0o077:
            log.warning("%s is readable by other users; run: sudo chmod 600 %s",
                        config_path, config_path)
        if os.geteuid() == 0 and st.st_uid != 0:
            log.warning("%s is not owned by root; run: sudo chown root:wheel %s",
                        config_path, config_path)
    except OSError:
        pass
    iface = cfg["wifi_interface"] or detect_wifi_interface()
    if iface:
        log.info("Wi-Fi interface: %s", iface)
    else:
        log.error("No Wi-Fi interface found; set wifi_interface in the config")
        ok = False
    api = TelegramAPI(cfg["telegram_bot_token"], cfg["telegram_chat_id"], cfg["telegram_api_base"])
    try:
        me = api.call("getMe")
        log.info("Telegram bot: @%s", me.get("username"))
        hook = api.call("getWebhookInfo")
        if hook.get("url"):
            log.error("The bot has a webhook set, so getUpdates won't work. Remove it with "
                      "https://api.telegram.org/bot<token>/deleteWebhook")
            ok = False
        chat = api.call("getChat", {"chat_id": cfg["telegram_chat_id"]})
        log.info("Telegram chat: %s", chat.get("username") or chat.get("title") or chat.get("id"))
    except TelegramError as e:
        log.error("Telegram check failed: %s", e)
        ok = False
    log.info("Config check %s", "passed" if ok else "FAILED")
    return 0 if ok else 1


def parse_args(argv):
    p = argparse.ArgumentParser(description="Ask on Telegram whether you're going home when the lid closes.")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"default {DEFAULT_CONFIG_PATH}")
    p.add_argument("--log-file", default=DEFAULT_LOG_PATH, help=f"default {DEFAULT_LOG_PATH}")
    p.add_argument("--dry-run", action="store_true",
                   help="log pmset/networksetup/caffeinate commands instead of running them")
    p.add_argument("--simulate-lid", choices=["open", "closed"],
                   help="ignore the real lid; start in this state, then type open/closed on stdin")
    p.add_argument("--check", action="store_true",
                   help="validate the config and Telegram access, then exit")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"gohome: {e}", file=sys.stderr)
        return 2
    log = setup_logging(None if args.check else args.log_file,
                        [cfg["telegram_bot_token"], cfg["hotspot_password"]])
    if args.check:
        return run_check(cfg, args.config, log)
    if not args.dry_run and os.geteuid() != 0:
        log.error("pmset needs root: run as a LaunchDaemon or with sudo, or use --dry-run")
        return 2

    def stop(signum, frame):
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        log.info("Received signal %d", signum)
        raise SystemExit(0)

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, stop)

    if args.simulate_lid:
        lid = SimulatedLid(args.simulate_lid == "closed", log)
        threading.Thread(target=lid.read_stdin, name="stdin", daemon=True).start()
    else:
        lid = IORegLid(log)
    system = MacSystem(cfg, log, dry_run=args.dry_run)
    api = TelegramAPI(cfg["telegram_bot_token"], cfg["telegram_chat_id"], cfg["telegram_api_base"])
    asker = TelegramAsker(api, log)
    app = GoHome(cfg, lid, system, asker, Clock(), log)
    log.info("gohome %s starting (dry_run=%s, simulate_lid=%s, wifi=%s, ssid=%s)", __version__,
             args.dry_run, args.simulate_lid or "no", system.iface, cfg["hotspot_ssid"])
    try:
        asker.start()
        app.start()
        while True:
            app.tick()
            time.sleep(POLL_INTERVAL)
    except SystemExit:
        return 0
    except Exception:
        log.exception("Crashed")
        return 1
    finally:
        app.shutdown()
        asker.stop()


if __name__ == "__main__":
    sys.exit(main())
