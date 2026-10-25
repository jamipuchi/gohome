"""End-to-end: runs gohome.py --dry-run --simulate-lid as a subprocess against a local fake
Telegram Bot API, drives the lid through stdin, and stops it with SIGTERM.
Takes about 30 s. Run: python3 -m unittest tests.test_e2e -v"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TOKEN = "123456:FAKE-TOKEN-should-never-be-logged"
CHAT = 4242


class FakeTelegram:
    """Just enough of the Bot API. When answer_with is set, a button press for the most
    recent question is queued as soon as the question is sent."""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates = []
        self.update_id = 500
        self.message_id = 0
        self.messages = {}          # message_id -> latest text
        self.calls = []
        self.answer_with = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                if f"/bot{TOKEN}/" not in self.path:
                    return self.reply({"ok": False, "description": "Unauthorized"}, 401)
                self.reply({"ok": True, "result": fake.handle(method, body)})

            def reply(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, method, body):
        with self.lock:
            self.calls.append(method)
            if method == "sendMessage":
                self.message_id += 1
                self.messages[self.message_id] = body["text"]
                if self.answer_with:
                    self.update_id += 1
                    self.updates.append({"update_id": self.update_id, "callback_query": {
                        "id": f"cb{self.update_id}", "data": self.answer_with,
                        "from": {"id": CHAT},
                        "message": {"message_id": self.message_id, "chat": {"id": CHAT}}}})
                return {"message_id": self.message_id, "chat": {"id": CHAT}}
            if method == "editMessageText":
                self.messages[body["message_id"]] = body["text"]
                return True
            if method == "answerCallbackQuery":
                return True
            if method == "getUpdates":
                offset = body.get("offset")
                if offset is not None and offset >= 0:
                    self.updates = [u for u in self.updates if u["update_id"] >= offset]
                pending = list(self.updates)
        if method == "getUpdates":
            if not pending and body.get("timeout"):
                time.sleep(1)   # a short "long poll"
            return pending
        return {}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram()
        self.addCleanup(self.tg.server.server_close)
        self.addCleanup(self.tg.server.shutdown)
        tmp = tempfile.mkdtemp()
        self.log_path = os.path.join(tmp, "gohome.log")
        self.config = os.path.join(tmp, "gohome.json")
        with open(self.config, "w") as f:
            json.dump({"telegram_bot_token": TOKEN, "telegram_chat_id": CHAT,
                       "hotspot_ssid": "Test Phone", "hotspot_password": "hotspot-secret-pw",
                       "wifi_interface": "en0", "ask_timeout_seconds": 6,
                       "skip_when_external_display": False,
                       "telegram_api_base": self.tg.url}, f)

    def start(self):
        self.mark = 0
        self.proc = subprocess.Popen(
            [sys.executable, "-I", os.path.join(ROOT, "gohome.py"), "--dry-run",
             "--simulate-lid", "open", "--config", self.config, "--log-file", self.log_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.proc.stdin.close)
        self.addCleanup(lambda: self.proc.poll() is None and self.proc.kill())
        self.wait_for("State START -> IDLE")

    def lid(self, state):
        self.mark = len(self.log())   # wait_for() only looks at lines logged after this
        self.proc.stdin.write(state + "\n")
        self.proc.stdin.flush()

    def log(self):
        try:
            with open(self.log_path) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def wait_for(self, text, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if text in self.log()[self.mark:]:
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {text!r}. Log:\n{self.log()}")

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(10), 0)

    def test_full_cycle(self):
        self.start()
        self.assertIn("would run: pmset -a disablesleep 1", self.log())

        # 1. going home
        self.tg.answer_with = "home"
        self.lid("closed")
        self.wait_for("State ASKING -> HOME")
        self.wait_for("Connected to hotspot")
        log = self.log()
        self.assertIn("would run: pmset displaysleepnow", log)
        self.assertIn("would run: caffeinate -i -w", log)
        self.assertIn("would run: networksetup -setairportnetwork en0 Test Phone ********", log)
        self.assertIn("Connected to hotspot", self.tg.messages[1])
        self.lid("open")
        self.wait_for("State HOME -> IDLE")

        # 2. not going home
        self.tg.answer_with = "not_home"
        self.lid("closed")
        self.wait_for("State ASKING -> SLEEPING (not going home)")
        self.wait_for("would run: pmset sleepnow")
        self.assertIn("Sleeping", self.tg.messages[2])
        self.lid("open")
        self.wait_for("State SLEEPING -> IDLE")

        # 3. no answer: times out (6 s in this config)
        self.tg.answer_with = None
        self.lid("closed")
        self.wait_for("State ASKING -> SLEEPING (no answer within 6 s)")
        self.assertIn("no answer", self.tg.messages[3])
        self.lid("open")
        self.wait_for("State SLEEPING -> IDLE")

        # 4. lid reopened while asking
        self.lid("closed")
        self.wait_for("Question sent (message 4)")
        self.lid("open")
        self.wait_for("State ASKING -> IDLE (lid opened while asking)")
        self.assertEqual(self.tg.messages[4], "↩️ Cancelled (lid opened)")

        self.stop()
        log = self.log()
        self.assertTrue(log.rstrip().endswith("Stopped; disablesleep set back to 0"), log[-300:])
        self.assertIn("would run: pmset -a disablesleep 0", log.split("Received signal 15")[1])
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("hotspot-secret-pw", log)

    def test_sigterm_while_asking_restores_sleep(self):
        self.start()
        self.lid("closed")
        self.wait_for("Question sent")
        self.stop()
        tail = self.log().split("Received signal 15")[1]
        self.assertIn("would run: pmset -a disablesleep 0", tail)
        self.assertEqual(self.tg.messages[1], "⏹ Helper stopped")

    def test_telegram_unreachable(self):
        with open(self.config) as f:
            cfg = json.load(f)
        cfg["telegram_api_base"] = "http://127.0.0.1:9"   # nothing listens here
        with open(self.config, "w") as f:
            json.dump(cfg, f)
        self.start()
        self.lid("closed")
        self.wait_for("Could not send the question")
        self.wait_for("State ASKING -> SLEEPING (Telegram unreachable)")
        self.stop()


if __name__ == "__main__":
    unittest.main()
