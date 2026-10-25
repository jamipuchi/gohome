# gohome

[![tests](https://github.com/jamipuchi/gohome/actions/workflows/test.yml/badge.svg)](https://github.com/jamipuchi/gohome/actions/workflows/test.yml)

**Close your MacBook lid, get asked on your phone whether you're going home.**

When the lid closes, gohome sends you a Telegram message with two buttons:

- **🏠 Going home:** the Mac stays awake with the screen locked and joins your iPhone's Personal Hotspot, so it's online on the way home (syncing, downloads, an agent finishing a task…).
- **🚫 Not going home, or no answer within 3 minutes:** the Mac goes to sleep as usual.

<p align="center">
  <code>💻 Lid closed. Going straight home?</code><br>
  <code>[ 🏠 Going home ]  [ 🚫 Not going home ]</code><br>
  ↓<br>
  <code>🏠 Going home: ✅ Connected to hotspot</code>
</p>

- One Python file, standard library only. You don't install any packages.
- No iOS app: the phone side is a Telegram bot you create in two minutes.
- Runs as a root LaunchDaemon, starts at boot, and turns normal sleep back on whenever it stops.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Day to day](#day-to-day)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Development and testing](#development-and-testing)

## Requirements

- A MacBook (Apple Silicon or Intel) with a recent macOS. Developed on macOS 27 (Apple Silicon); uses only long-standing `pmset`, `ioreg` and `networksetup` commands.
- The Command Line Tools, for `/usr/bin/python3` 3.8 or later: `xcode-select --install`.
- An iPhone with Personal Hotspot, and a Telegram account.
- An admin password: `pmset` needs root, so gohome runs as a system LaunchDaemon.

## Quick start

### 1. Create a Telegram bot

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot`, and pick a name and a username ending in `bot`.
2. BotFather replies with a token like `123456789:AA…`. Keep it secret: anyone with it controls your bot.
3. Open a chat with your new bot, tap **Start**, and send it any message. Bots can't message you until you've written to them first.

### 2. Get your chat ID

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'
```

The `"id"` under `"chat"` is your chat ID, a positive number for a private chat. If the result is empty, send the bot another message and run the command again. Do this before installing, because the running helper consumes the bot's updates.

### 3. Change two macOS settings

- **System Settings → Lock Screen →** "Require password after screen saver begins or display is turned off" → **Immediately**. gohome locks the screen with `pmset displaysleepnow`, which only locks if this is set.
- **System Settings → Wi-Fi →** "Ask to join hotspots" → **Automatic**. With the same Apple ID on Mac and iPhone (Instant Hotspot), macOS can join the hotspot by itself too.

Join your iPhone hotspot once by hand from the Wi-Fi menu. After that macOS remembers its password, and you can leave `hotspot_password` empty.

### 4. Configure and install

```bash
git clone https://github.com/jamipuchi/gohome.git
cd gohome
cp gohome.example.json gohome.json && chmod 600 gohome.json
nano gohome.json              # token, chat ID, hotspot name
python3 gohome.py --check --config gohome.json
sudo ./install.sh gohome.json
```

- **Hotspot name:** it's your iPhone's name, under **Settings → General → About → Name**. Copy it exactly: iPhone names usually contain a curly apostrophe (`’`), not a straight one (`'`). `networksetup -listpreferredwirelessnetworks en0` prints the exact spelling once the Mac has joined it.
- **`--check`:** validates the config, the bot token and the chat ID, and makes sure the bot has no webhook set. It sends no message.
- **`install.sh`:**
  1. Copies `gohome.py` to `/usr/local/libexec/` and the config to `/usr/local/etc/gohome.json` (mode 600, owned by root).
  2. Runs the same check.
  3. Installs `/Library/LaunchDaemons/com.jamipuchi.gohome.plist` and starts it.
- `gohome.json` is in `.gitignore`, so your token can't be committed by accident. After installing you can delete the local copy.

When it's done, `pmset -g | grep SleepDisabled` shows `1`. macOS may show a "Background Items Added" notification for `python3`. That's gohome. Leave it allowed.

Before installing, you can try the whole flow with `--dry-run`, which runs nothing on the Mac. See [Development and testing](#development-and-testing).

## How it works

macOS sleeps the moment the lid closes. No process can veto or delay that, and `caffeinate` doesn't prevent it. So gohome inverts the default: while it runs, `pmset -a disablesleep 1` is set, so closing the lid no longer sleeps the Mac. gohome checks the lid every 2 seconds (`ioreg … AppleClamshellState`) and puts the Mac to sleep itself when that's the right answer.

```
IDLE (lid open, disablesleep 1)
  └─ lid closes ─┬─ external display connected ─> DOCKED (clamshell mode, no question)
                 └─> ASKING: lock screen, send question, wait up to 180 s
                       ├─ lid opens ──────────────> "Cancelled (lid opened)" -> IDLE
                       ├─ 🏠 Going home ──────────> HOME
                       └─ 🚫 / timeout / no network > SLEEPING
HOME: caffeinate -i, join hotspot (retry every 10 s for 5 min), report result in Telegram
  ├─ lid opens ───────────────────────────────────> IDLE
  └─ 90 min awake, or battery ≤ 10% ──────────────> SLEEPING
SLEEPING: edit message, disablesleep 0, pmset sleepnow
  └─ lid opens (after wake) ──────────────────────> disablesleep 1 -> IDLE
```

### Safety

- **Whenever gohome exits, it turns sleep back on first.** That covers SIGTERM, Ctrl-C, a crash and `launchctl bootout`: it sets `disablesleep 0` before anything else. If it crashes, launchd starts it again within 10 s.
- **`caffeinate` can't outlive gohome.** It runs with `-w <pid>`, so it ends with gohome even after a `kill -9`.
- **The awake time is capped.** It stays awake for 90 minutes at most, and sleeps earlier if it's on battery at 10% or below.
- **Telegram messages are filtered.** Only button presses from your chat, on the current question, count. Everything else is ignored.
- **Secrets stay out of the log.** The bot token and hotspot password are masked in every log line.

### Design notes

- **It re-arms when the lid opens, not on wake.** macOS wakes briefly with the lid closed (Power Nap, network maintenance). Re-enabling `disablesleep 1` on every wake would keep a Mac awake in your bag. So after it sleeps the Mac, gohome waits for the lid to open before it re-arms, and a wake with the lid still closed never asks a second time. Wakes are still detected (by the gap between `CLOCK_MONOTONIC` and `CLOCK_UPTIME_RAW`) and logged. If the Mac stays awake with the lid closed, gohome sends `sleepnow` again: after 60 s when it never slept, or after 5 min following a wake.
- **Joining the hotspot is confirmed by IP address.** On recent macOS, `networksetup -getairportnetwork` claims you're "not associated" even when you are, and `-setairportnetwork` often exits 0 when it failed. So gohome reads what `networksetup` prints and then waits for the Wi-Fi interface to get an address on the iPhone hotspot subnet (`172.20.10.x`).
- **Clamshell mode is respected.** With an external display connected, closing the lid sends no question. If the display is unplugged while the lid is still closed, gohome asks then.
- **A network blip doesn't skip the question.** If Telegram can't be reached, gohome retries sending the question every 15 s until the 3 minutes are up, then sleeps.

### Side effects to know about

- **While gohome runs, the Mac doesn't sleep on its own with the lid open.** `disablesleep 1` blocks all system sleep, idle sleep included. The display still turns off and locks on its normal schedule. To sleep it, close the lid and answer 🚫, or just wait.
- **A Mac that stays awake in a bag gets warm.** Keep `max_awake_minutes` reasonable.
- **The hotspot password is visible to other local users.** It's passed to `networksetup` on the command line, so it shows up in `ps` for a moment. Leave `hotspot_password` empty if your Mac already remembers the hotspot.

## Configuration

`/usr/local/etc/gohome.json` (see [`gohome.example.json`](gohome.example.json)):

| Key | Default | Meaning |
|---|---|---|
| `telegram_bot_token` | required | From BotFather |
| `telegram_chat_id` | required | Your chat with the bot |
| `hotspot_ssid` | required | Your iPhone's hotspot name, spelled exactly |
| `hotspot_password` | `""` | Empty uses the password macOS already remembers |
| `wifi_interface` | auto | Usually `en0`; see `networksetup -listallhardwareports` |
| `ask_timeout_seconds` | `180` | How long to wait for an answer |
| `max_awake_minutes` | `90` | How long HOME keeps the Mac awake at most |
| `hotspot_retry_seconds` | `10` | Delay between join attempts |
| `hotspot_give_up_seconds` | `300` | Stop trying to join after this long |
| `hotspot_subnet_prefix` | `"172.20.10."` | Address prefix that proves we're on the hotspot. Empty trusts `networksetup` |
| `min_battery_percent` | `10` | In HOME, sleep when on battery at or below this |
| `skip_when_external_display` | `true` | Don't ask when an external display is connected |

Unknown keys are rejected, so a typo doesn't go unnoticed. After editing the config, restart the helper:

```bash
sudo nano /usr/local/etc/gohome.json
sudo launchctl kickstart -k system/com.jamipuchi.gohome
```

## Day to day

| What | Command |
|---|---|
| Watch the log | `tail -f /var/log/gohome.log` |
| Is it running? | `sudo launchctl print system/com.jamipuchi.gohome \| grep -E 'state\|pid'` |
| Is lid-close sleep off? | `pmset -g \| grep SleepDisabled` (`1` while gohome runs) |
| Pause it until next boot | `sudo launchctl bootout system/com.jamipuchi.gohome` |
| Start it again | `sudo launchctl bootstrap system /Library/LaunchDaemons/com.jamipuchi.gohome.plist` |
| Restart after a config change | `sudo launchctl kickstart -k system/com.jamipuchi.gohome` |

- **`/var/log/gohome.log`** has a timestamp on every line and records every state change and every hotspot attempt. Secrets are masked. It rotates at 1 MB, keeping 3 old files.
- **`/var/log/gohome.stderr.log`** catches anything printed before logging starts, such as a config error.

## Troubleshooting

**The Mac won't sleep when I close the lid, and gohome isn't running.** Turn lid sleep back on:

```bash
sudo pmset -a disablesleep 0
```

**No question arrives when I close the lid.**
- Read `tail /var/log/gohome.log` and `/var/log/gohome.stderr.log`.
- Check that gohome is allowed under **System Settings → General → Login Items & Extensions** (it's listed as `python3`).
- Run `python3 /usr/local/libexec/gohome.py --check` with `sudo` to test the token and chat.
- With an external display connected, no question is sent on purpose; the log says `DOCKED`.

**"Hotspot not found".**
- On the iPhone, keep **Settings → Personal Hotspot → Allow Others to Join** on. The iPhone doesn't always broadcast its hotspot, so gohome retries for 5 minutes.
- Check that `hotspot_ssid` matches exactly, including the curly apostrophe (`’`).
- The log shows what `networksetup` printed for each attempt.

**It asked, but my tap did nothing.** The buttons only work on the current question. Older messages are edited to show their outcome and their buttons are removed. If another program reads the same bot's updates, it competes with gohome: one bot per helper.

## Uninstall

```bash
sudo ./uninstall.sh                # removes the daemon, script, config and logs
sudo ./uninstall.sh --keep-config  # keeps /usr/local/etc/gohome.json
```

Both run `pmset -a disablesleep 0`, so closing the lid sleeps the Mac again.

## Development and testing

```bash
python3 -m unittest -v
```

- **`tests/test_gohome.py`** tests the state machine with a fake lid, clock, Mac and Telegram. It covers yes, no, timeout, lid reopened while asking, network down, network recovering, maximum awake time, hotspot retries and giving up, low battery, external display, a lid closed at startup, one question per close after a wake, `sleepnow` retries, and cleanup on shutdown. It also tests filtering of Telegram callbacks, secret masking and config validation.
- **`tests/test_e2e.py`** runs the real script with `--dry-run --simulate-lid` against a local fake Telegram Bot API server. It drives the lid through stdin, then stops the script with SIGTERM and checks that sleep is turned back on and no secret reached the log. It takes about 40 s.

CI runs both on every push ([workflow](.github/workflows/test.yml)).

### Dry run against your real bot

`--dry-run` logs every `pmset`, `networksetup` and `caffeinate` command instead of running it, and doesn't need root. `--simulate-lid` ignores the real lid and reads `open` / `closed` (or `o` / `c`) from stdin:

```bash
python3 gohome.py --dry-run --simulate-lid open --config gohome.json --log-file gohome-dry-run.log
```

Type `closed` and press Enter. The question arrives on your phone. Tap a button, or type `open` to cancel. Press Ctrl-C to stop; the last log line is `Stopped; disablesleep set back to 0`. If the installed helper is running, pause it first (see [Day to day](#day-to-day)), because two programs reading one bot's updates conflict.

### Manual checklist on a real Mac

Keep `tail -f /var/log/gohome.log` open, and use the Mac on battery with no external display.

1. **Before installing:** `pmset -g | grep -i sleep` shows `SleepDisabled 0`, or no such line.
2. **After installing:** the same command shows `SleepDisabled 1`, and the log shows `State START -> IDLE`.
3. **Going home:** close the lid at your desk. The question arrives within seconds and the log shows `IDLE -> ASKING`. Tap 🏠 with your hotspot on. The message changes to "✅ Connected to hotspot". Open the lid: the lock screen asks for your password, and the log shows `HOME -> IDLE`.
4. **Not going home:** close the lid and tap 🚫. The message says "😴 Sleeping (not going home)", and the Mac is asleep within seconds. `pmset -g log | grep -i "Entering Sleep"` confirms it afterwards. Open the lid: the log shows `Woke up after sleeping … s` and `SLEEPING -> IDLE`, and `SleepDisabled` is `1` again.
5. **Timeout:** close the lid and don't answer. After 3 minutes the message says "no answer within 180 s" and the Mac sleeps.
6. **Cancel:** close the lid, then open it before answering. The message says "↩️ Cancelled (lid opened)" and the Mac doesn't sleep.
7. **No network:** turn Wi-Fi off, then close the lid. After 3 minutes the log shows `SLEEPING (Telegram unreachable)`.
8. **One question per close:** after step 4, leave the lid closed for 30+ minutes. Power Nap wakes are logged as `Woke up…`, and no second question arrives.
9. **Maximum awake time:** set `"max_awake_minutes": 2`, restart the helper, close the lid, and tap 🏠. The Mac sleeps after 2 minutes. Set it back afterwards.
10. **External display:** close the lid with a display connected. No question arrives, and the log shows `DOCKED`.
11. **Stop restores sleep:** `sudo launchctl bootout system/com.jamipuchi.gohome`, then `SleepDisabled` shows `0`.
12. **Survives a reboot:** restart the Mac, and the log shows a new `starting` line with `SleepDisabled 1`.

### Project layout

| File | What it is |
|---|---|
| `gohome.py` | The daemon: config, lid, macOS actions, Telegram client, state machine |
| `com.jamipuchi.gohome.plist` | LaunchDaemon (`RunAtLoad`, `KeepAlive`) |
| `install.sh` / `uninstall.sh` | Install, load, unload, and restore sleep |
| `gohome.example.json` | Config template |
| `tests/` | Unit tests and the end-to-end dry-run test |

## License

[MIT](LICENSE)
