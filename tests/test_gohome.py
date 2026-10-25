"""State machine tests with a fake lid, clock, Mac and Telegram. Run: python3 -m unittest -v"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gohome as g  # noqa: E402

LOG = logging.getLogger("gohome-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class FakeClock:
    def __init__(self):
        self._now = 1000.0
        self._awake = 1000.0

    def now(self):
        return self._now

    def awake(self):
        return self._awake

    def advance(self, seconds, asleep=False):
        self._now += seconds
        if not asleep:
            self._awake += seconds


class FakeLid:
    def __init__(self, closed=False):
        self.closed = closed

    def is_closed(self):
        return self.closed


class FakeSystem:
    def __init__(self):
        self.calls = []
        self.sleep_disabled = None
        self.keepawake = False
        self.join_results = []      # consumed per attempt; empty means fail
        self.external_display = False
        self.battery_level = (80, False)

    def lock_screen(self):
        self.calls.append("lock")

    def set_sleep_disabled(self, disabled):
        self.calls.append(f"disablesleep {int(disabled)}")
        self.sleep_disabled = disabled

    def sleep_now(self):
        self.calls.append("sleepnow")

    def start_keepawake(self):
        self.calls.append("caffeinate start")
        self.keepawake = True

    def stop_keepawake(self):
        if self.keepawake:
            self.calls.append("caffeinate stop")
        self.keepawake = False

    def join_hotspot(self):
        self.calls.append("join")
        return self.join_results.pop(0) if self.join_results else False

    def battery(self):
        return self.battery_level

    def external_display_connected(self):
        return self.external_display


class FakeMessenger:
    def __init__(self, clock):
        self.clock = clock
        self.network_up = True
        self.sent = []
        self.answers = {}       # msg_id -> (answer, available_at)
        self.edits = []         # (msg_id, text)
        self.finished = []
        self.next_answer = None  # (answer, delay) applied to the next question

    def ask(self):
        if not self.network_up:
            return None
        msg_id = 100 + len(self.sent)
        self.sent.append(msg_id)
        if self.next_answer:
            answer, delay = self.next_answer
            self.answers[msg_id] = (answer, self.clock.now() + delay)
            self.next_answer = None
        return msg_id

    def answer(self, msg_id):
        if msg_id in self.finished:
            return None
        a = self.answers.get(msg_id)
        return a[0] if a and self.clock.now() >= a[1] else None

    def finish(self, msg_id, text):
        self.finished.append(msg_id)
        self.edits.append((msg_id, text))

    def edit(self, msg_id, text):
        self.edits.append((msg_id, text))

    def last_text(self):
        return self.edits[-1][1] if self.edits else None


class Harness(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(g.DEFAULTS, telegram_bot_token="t", telegram_chat_id=1, hotspot_ssid="Phone")
        self.clock = FakeClock()
        self.lid = FakeLid(closed=False)
        self.system = FakeSystem()
        self.tg = FakeMessenger(self.clock)
        self.app = g.GoHome(self.cfg, self.lid, self.system, self.tg, self.clock, LOG)
        self.app.start()
        self.app.tick()

    def run_for(self, seconds, step=2.0):
        t = 0.0
        while t < seconds:
            self.clock.advance(step)
            self.app.tick()
            t += step

    def close_lid(self):
        self.lid.closed = True
        self.clock.advance(2)
        self.app.tick()

    def open_lid(self):
        self.lid.closed = False
        self.clock.advance(2)
        self.app.tick()


class StateMachineTests(Harness):
    def test_starts_idle_with_sleep_disabled(self):
        self.assertEqual(self.app.state, g.IDLE)
        self.assertTrue(self.system.sleep_disabled)

    def test_lid_close_locks_and_asks(self):
        self.close_lid()
        self.assertEqual(self.app.state, g.ASKING)
        self.assertIn("lock", self.system.calls)
        self.assertEqual(len(self.tg.sent), 1)

    def test_going_home(self):
        self.tg.next_answer = (g.ANSWER_HOME, 10)
        self.system.join_results = [True]
        self.close_lid()
        self.run_for(12)
        self.assertEqual(self.app.state, g.HOME)
        self.assertTrue(self.system.keepawake)
        self.assertTrue(self.system.sleep_disabled)
        self.assertNotIn("sleepnow", self.system.calls)
        self.assertIn("join", self.system.calls)
        self.assertIn("Connected to hotspot", self.tg.last_text())
        self.open_lid()
        self.assertEqual(self.app.state, g.IDLE)
        self.assertFalse(self.system.keepawake)
        self.assertTrue(self.system.sleep_disabled)

    def test_not_going_home_sleeps(self):
        self.tg.next_answer = (g.ANSWER_NOT_HOME, 4)
        self.close_lid()
        self.run_for(6)
        self.assertEqual(self.app.state, g.SLEEPING)
        self.assertEqual(self.system.calls[-2:], ["disablesleep 0", "sleepnow"])
        self.assertIn("Sleeping", self.tg.last_text())
        self.assertIn("not going home", self.tg.last_text())

    def test_timeout_sleeps_after_180s_not_before(self):
        self.close_lid()
        self.run_for(176)
        self.assertEqual(self.app.state, g.ASKING)
        self.assertNotIn("sleepnow", self.system.calls)
        self.run_for(4)
        self.assertEqual(self.app.state, g.SLEEPING)
        self.assertIn("sleepnow", self.system.calls)
        self.assertIn("no answer", self.tg.last_text())

    def test_lid_reopened_while_asking_cancels(self):
        self.close_lid()
        self.run_for(20)
        self.open_lid()
        self.assertEqual(self.app.state, g.IDLE)
        self.assertEqual(self.tg.last_text(), "↩️ Cancelled (lid opened)")
        self.assertNotIn("sleepnow", self.system.calls)
        self.assertTrue(self.system.sleep_disabled)

    def test_answer_after_cancel_is_ignored(self):
        self.tg.next_answer = (g.ANSWER_HOME, 30)
        self.close_lid()
        self.run_for(10)
        self.open_lid()
        self.run_for(40)
        self.assertEqual(self.app.state, g.IDLE)
        self.assertNotIn("caffeinate start", self.system.calls)

    def test_network_down_sleeps_after_timeout(self):
        self.tg.network_up = False
        self.close_lid()
        self.run_for(176)
        self.assertEqual(self.app.state, g.ASKING)
        self.run_for(4)
        self.assertEqual(self.app.state, g.SLEEPING)
        self.assertEqual(self.tg.edits, [])   # nothing to edit, nothing sent
        self.assertEqual(self.system.calls[-2:], ["disablesleep 0", "sleepnow"])

    def test_network_recovers_during_window(self):
        self.tg.network_up = False
        self.close_lid()
        self.run_for(20)
        self.tg.network_up = True
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.run_for(16)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.app.state, g.HOME)

    def test_max_awake_time_sleeps(self):
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.system.join_results = [True]
        self.close_lid()
        self.run_for(90 * 60 - 10, step=10)
        self.assertEqual(self.app.state, g.HOME)
        self.run_for(20, step=10)
        self.assertEqual(self.app.state, g.SLEEPING)
        self.assertFalse(self.system.keepawake)
        self.assertEqual(self.system.calls[-2:], ["disablesleep 0", "sleepnow"])
        self.assertIn("maximum", self.tg.last_text())

    def test_hotspot_retries_then_succeeds(self):
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.system.join_results = [False, False, True]
        self.close_lid()
        self.run_for(40)
        self.assertEqual(self.system.calls.count("join"), 3)
        self.assertIn("Connected to hotspot", self.tg.last_text())

    def test_hotspot_gives_up_after_5_minutes(self):
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.close_lid()
        self.run_for(400)
        joins = self.system.calls.count("join")
        self.assertGreaterEqual(joins, 29)
        self.assertLessEqual(joins, 32)
        self.assertIn("Hotspot not found", self.tg.last_text())
        self.assertEqual(self.app.state, g.HOME)  # still awake, still going home

    def test_low_battery_in_home_sleeps(self):
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.system.join_results = [True]
        self.close_lid()
        self.run_for(10)
        self.system.battery_level = (8, False)
        self.run_for(62)
        self.assertEqual(self.app.state, g.SLEEPING)
        self.assertIn("battery", self.tg.last_text())

    def test_low_battery_on_ac_keeps_awake(self):
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.system.join_results = [True]
        self.system.battery_level = (5, True)
        self.close_lid()
        self.run_for(120)
        self.assertEqual(self.app.state, g.HOME)

    def test_one_question_per_close_after_wake(self):
        self.tg.next_answer = (g.ANSWER_NOT_HOME, 0)
        self.close_lid()
        self.run_for(4)
        self.assertEqual(self.app.state, g.SLEEPING)
        self.clock.advance(3600, asleep=True)   # asleep for an hour
        self.run_for(120)                        # woke (e.g. Power Nap) with the lid still closed
        self.assertEqual(self.app.state, g.SLEEPING)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertFalse(self.system.sleep_disabled)   # macOS can put it back to sleep
        self.open_lid()
        self.assertEqual(self.app.state, g.IDLE)
        self.assertTrue(self.system.sleep_disabled)
        self.close_lid()
        self.assertEqual(len(self.tg.sent), 2)         # a new close asks again

    def test_sleepnow_retried_if_mac_stays_awake(self):
        self.tg.next_answer = (g.ANSWER_NOT_HOME, 0)
        self.close_lid()
        self.run_for(4)
        self.assertEqual(self.system.calls.count("sleepnow"), 1)
        self.run_for(62)
        self.assertEqual(self.system.calls.count("sleepnow"), 2)

    def test_awake_long_after_wake_with_lid_closed_sleeps_again(self):
        self.tg.next_answer = (g.ANSWER_NOT_HOME, 0)
        self.close_lid()
        self.run_for(4)
        self.clock.advance(600, asleep=True)
        self.run_for(200)
        self.assertEqual(self.system.calls.count("sleepnow"), 1)
        self.run_for(120)
        self.assertEqual(self.system.calls.count("sleepnow"), 2)

    def test_lid_closed_at_startup_asks(self):
        lid = FakeLid(closed=True)
        app = g.GoHome(self.cfg, lid, self.system, self.tg, self.clock, LOG)
        app.start()
        app.tick()
        self.assertEqual(app.state, g.ASKING)

    def test_external_display_docked_no_question(self):
        self.system.external_display = True
        self.close_lid()
        self.run_for(300)
        self.assertEqual(self.app.state, g.DOCKED)
        self.assertEqual(self.tg.sent, [])
        self.system.external_display = False
        self.run_for(12)
        self.assertEqual(self.app.state, g.ASKING)
        self.assertEqual(len(self.tg.sent), 1)

    def test_external_display_check_can_be_disabled(self):
        self.cfg["skip_when_external_display"] = False
        self.system.external_display = True
        self.close_lid()
        self.assertEqual(self.app.state, g.ASKING)

    def test_shutdown_restores_sleep(self):
        self.tg.next_answer = (g.ANSWER_HOME, 0)
        self.close_lid()
        self.run_for(4)
        self.assertEqual(self.app.state, g.HOME)
        self.app.shutdown()
        self.assertFalse(self.system.sleep_disabled)
        self.assertFalse(self.system.keepawake)
        self.assertEqual(self.system.calls[-2:], ["disablesleep 0", "caffeinate stop"])

    def test_shutdown_while_asking_edits_message(self):
        self.close_lid()
        self.app.shutdown()
        self.assertFalse(self.system.sleep_disabled)
        self.assertEqual(self.tg.last_text(), "⏹ Helper stopped")


class FakeAPI:
    chat_id = 42

    def __init__(self):
        self.acks = []

    def answer_callback(self, cid, text):
        self.acks.append((cid, text))


class TelegramAskerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.asker = g.TelegramAsker(self.api, LOG)
        self.asker._active = 7

    def cb(self, chat=42, message_id=7, data=g.ANSWER_HOME):
        return {"update_id": 1, "callback_query": {
            "id": "cb", "data": data, "from": {"id": chat},
            "message": {"message_id": message_id, "chat": {"id": chat}}}}

    def test_accepts_matching_callback(self):
        self.asker.handle_update(self.cb())
        self.assertEqual(self.asker.answer(7), g.ANSWER_HOME)
        self.assertEqual(self.api.acks, [("cb", "Got it")])

    def test_ignores_other_chat(self):
        self.asker.handle_update(self.cb(chat=99))
        self.assertIsNone(self.asker.answer(7))
        self.assertEqual(self.api.acks, [])

    def test_ignores_other_message(self):
        self.asker.handle_update(self.cb(message_id=6))
        self.assertIsNone(self.asker.answer(7))
        self.assertIsNone(self.asker.answer(6))
        self.assertEqual(self.api.acks, [("cb", "This question has expired")])

    def test_ignores_unknown_data(self):
        self.asker.handle_update(self.cb(data="something"))
        self.assertIsNone(self.asker.answer(7))

    def test_ignores_plain_messages(self):
        self.asker.handle_update({"update_id": 2, "message": {"text": "hi"}})
        self.assertIsNone(self.asker.answer(7))

    def test_ignores_after_finish(self):
        self.api.edit_message = lambda *a: None
        self.asker.finish(7, "done")
        self.asker.handle_update(self.cb())
        self.assertIsNone(self.asker.answer(7))


class ParsingTests(unittest.TestCase):
    def test_clamshell(self):
        self.assertTrue(g.parse_clamshell('  |   "AppleClamshellState" = Yes\n'))
        self.assertFalse(g.parse_clamshell('  |   "AppleClamshellState" = No\n'))
        self.assertIsNone(g.parse_clamshell(""))

    def test_battery(self):
        out = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t45%; discharging;"
        self.assertEqual(g.parse_battery(out), (45, False))
        out = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t9%; charging;"
        self.assertEqual(g.parse_battery(out), (9, True))

    def test_redaction(self):
        import io
        stream = io.StringIO()
        log = logging.getLogger("gohome-redact-test")
        log.propagate = False
        h = logging.StreamHandler(stream)
        h.addFilter(g.RedactSecrets(["123:SECRETTOKEN", "hunter22"]))
        log.addHandler(h)
        log.warning("url https://x/bot%s/getMe pw=%s", "123:SECRETTOKEN", "hunter22")
        self.assertNotIn("SECRETTOKEN", stream.getvalue())
        self.assertNotIn("hunter22", stream.getvalue())
        self.assertIn("********", stream.getvalue())


class ConfigTests(unittest.TestCase):
    def write(self, text):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_example_config_loads(self):
        cfg = g.load_config(os.path.join(os.path.dirname(__file__), "..", "gohome.example.json"))
        self.assertEqual(cfg["ask_timeout_seconds"], 180)
        self.assertEqual(cfg["max_awake_minutes"], 90)

    def test_missing_required(self):
        with self.assertRaises(g.ConfigError):
            g.load_config(self.write('{"telegram_bot_token": "x"}'))

    def test_unknown_key(self):
        with self.assertRaises(g.ConfigError):
            g.load_config(self.write(
                '{"telegram_bot_token": "x", "telegram_chat_id": 1, "hotspot_ssid": "p", "typo": 1}'))

    def test_chat_id_string(self):
        cfg = g.load_config(self.write(
            '{"telegram_bot_token": "x", "telegram_chat_id": "-100123", "hotspot_ssid": "p"}'))
        self.assertEqual(cfg["telegram_chat_id"], -100123)


if __name__ == "__main__":
    unittest.main()
