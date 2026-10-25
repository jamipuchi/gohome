# gohome

When you close your MacBook lid, gohome sends a Telegram message to your iPhone asking whether you're going straight home:

- **🏠 Going home:** the Mac stays awake with the screen locked and joins your iPhone's Personal Hotspot, so it's online on the way.
- **🚫 Not going home, or no answer within 3 minutes:** the Mac goes to sleep as usual.

It's a single Python 3 script that uses only the standard library and runs as a root LaunchDaemon. You don't need an iOS app, only a Telegram bot.

## How it works

macOS sleeps the moment the lid closes, and no process can veto or delay that. So gohome inverts the default: while it runs, `pmset -a disablesleep 1` is set, so closing the lid no longer sleeps the Mac. gohome checks the lid every 2 seconds (`ioreg … AppleClamshellState`) and puts the Mac to sleep itself when that's the right answer.

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

Whenever gohome exits (SIGTERM, Ctrl-C, a crash, `launchctl bootout`), it sets `disablesleep 0` before doing anything else, so the Mac can't be left unable to sleep. `caffeinate` runs with `-w <pid>`, so it ends with gohome even after a `kill -9`.

### Where this differs from the original brief

- **The helper re-arms when the lid opens, not on wake.** macOS wakes briefly with the lid closed (Power Nap, network maintenance). If the helper set `disablesleep 1` on every wake, the Mac would stay awake in your bag. So after it sleeps the Mac, it waits for the lid to open before it re-arms, and a wake with the lid still closed never asks a second time. Wakes are still detected (the `CLOCK_MONOTONIC` vs `CLOCK_UPTIME_RAW` gap) and logged. If the Mac stays awake with the lid closed, gohome sends `sleepnow` again: after 60 s when it never slept, or after 5 min following a wake.
- **Joining the hotspot is confirmed by IP address.** On macOS 15 and later, `networksetup -getairportnetwork` returns "You are not associated with an AirPort network" even when you're connected, and `-setairportnetwork` often exits 0 when it failed. So gohome reads what `networksetup` prints and then waits for the Wi-Fi interface to get an address on the iPhone hotspot subnet (`172.20.10.x`, set by `hotspot_subnet_prefix`).
- **Extras:**
  - With an external display connected, the lid close is treated as clamshell mode and no question is sent (`skip_when_external_display`). If the display is unplugged while the lid is still closed, gohome asks then.
  - In HOME, gohome sleeps the Mac when it's on battery at or below `min_battery_percent`.
  - If Telegram can't be reached, gohome retries sending the question every 15 s within the 3-minute window instead of waiting out a single failure.

### Side effects to know about

- **While gohome runs, the Mac doesn't sleep on its own with the lid open.** `disablesleep 1` blocks all system sleep, idle sleep included. The display still turns off and locks on its normal schedule. Close the lid and answer 🚫 (or just wait) to sleep it.
- **A Mac that stays awake in a bag gets warm.** That's why `max_awake_minutes` exists. Keep it reasonable.
- **The hotspot password is visible to other local users.** It's passed to `networksetup` on the command line, so it shows up in `ps` for a moment. Leave `hotspot_password` empty if your Mac already remembers the hotspot. It does once you've joined it by hand.

## Setup

### 1. Create the Telegram bot

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and a username ending in `bot`.
2. BotFather replies with a token like `123456789:AA…`. That's your `telegram_bot_token`. Keep it secret.
3. Open a chat with your new bot and send it any message (for example `hi`). Bots can't message you until you've written to them.

### 2. Get your chat ID

Do this **before** installing. Once gohome is running, it consumes the bot's updates.

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'
```

The `"id"` under `"chat"` is your `telegram_chat_id`, a positive number for a private chat. If the result is empty, send the bot another message and run the command again.

### 3. Make "lock" really lock

gohome locks the screen with `pmset displaysleepnow`, which only locks if a password is required right away:

**System Settings → Lock Screen → "Require password after screen saver begins or display is turned off" → Immediately**

### 4. Let the Mac join your hotspot by itself (recommended)

If your Mac and iPhone use the same Apple ID, macOS can join the hotspot on its own (Instant Hotspot):

**System Settings → Wi-Fi → "Ask to join hotspots" → Automatic**

On the iPhone, keep Personal Hotspot turned on (Settings → Personal Hotspot → Allow Others to Join). An iPhone doesn't always broadcast its hotspot, which is why gohome retries every 10 s for 5 minutes and logs each attempt. If the Mac already joined through Instant Hotspot, gohome sees the `172.20.10.x` address and reports success.

To find your hotspot's SSID, look at the name shown in **Settings → General → About → Name** on the iPhone. To find the Wi-Fi interface, run `networksetup -listallhardwareports` (it's usually `en0`).

### 5. Install

```bash
git clone https://github.com/jamipuchi/gohome.git
cd gohome
sudo ./install.sh
```

The first run creates `/usr/local/etc/gohome.json` (mode 600, owned by root) from `gohome.example.json` and stops. Fill it in:

```bash
sudo nano /usr/local/etc/gohome.json
```

Then run `sudo ./install.sh` again. It copies `gohome.py` to `/usr/local/libexec/`, runs `gohome.py --check` (config, Telegram token, chat ID, no webhook set), installs `/Library/LaunchDaemons/com.jamipuchi.gohome.plist`, and loads it with `launchctl bootstrap system`. Afterwards `pmset -g | grep SleepDisabled` shows `1`.

`/usr/bin/python3` needs the Command Line Tools (`xcode-select --install`). The installer checks for them.

### Configuration

| Key | Default | Meaning |
|---|---|---|
| `telegram_bot_token` | required | From BotFather |
| `telegram_chat_id` | required | Your chat with the bot |
| `hotspot_ssid` | required | Your iPhone's hotspot name |
| `hotspot_password` | `""` | Empty uses the password macOS already remembers |
| `wifi_interface` | auto | Usually `en0` |
| `ask_timeout_seconds` | `180` | How long to wait for an answer |
| `max_awake_minutes` | `90` | How long HOME keeps the Mac awake at most |
| `hotspot_retry_seconds` | `10` | Delay between join attempts |
| `hotspot_give_up_seconds` | `300` | Stop trying to join after this long |
| `hotspot_subnet_prefix` | `"172.20.10."` | Address prefix that proves we're on the hotspot. Empty trusts `networksetup` |
| `min_battery_percent` | `10` | In HOME, sleep when on battery at or below this |
| `skip_when_external_display` | `true` | Don't ask when an external display is connected |

To change the config later, edit the file and restart the daemon:

```bash
sudo launchctl kickstart -k system/com.jamipuchi.gohome
```

### Logs

- `/var/log/gohome.log`: timestamps, every state change, every hotspot attempt. Secrets are masked. Rotated at 1 MB, keeping 3 old files.
- `/var/log/gohome.stderr.log`: anything printed before logging starts, such as a config error.

```bash
tail -f /var/log/gohome.log
```

### Uninstall

```bash
sudo ./uninstall.sh                # removes the daemon, script, config and logs
sudo ./uninstall.sh --keep-config  # keeps /usr/local/etc/gohome.json
```

Both run `pmset -a disablesleep 0`, so closing the lid puts the Mac to sleep again.

If the Mac ever won't sleep when you close the lid and gohome isn't running, run:

```bash
sudo pmset -a disablesleep 0
```

## Testing

### Unit and end-to-end tests

```bash
python3 -m unittest -v
```

- `tests/test_gohome.py` tests the state machine with a fake lid, fake clock, fake Mac and fake Telegram. It covers yes, no, timeout, lid reopened while asking, network down, network recovering, maximum awake time, hotspot retries and giving up, low battery, external display, a lid closed at startup, one question per close after a wake, `sleepnow` retries, and cleanup on shutdown. It also tests filtering of Telegram callbacks (wrong chat, wrong message, unknown button), secret redaction and config validation.
- `tests/test_e2e.py` runs the real script with `--dry-run --simulate-lid` against a local fake Telegram Bot API server. It drives the lid through stdin, then stops the script with SIGTERM and checks that `disablesleep 0` ran and that no secret reached the log. It takes about 40 s.

### Dry run against your real bot

`--dry-run` logs every `pmset`, `networksetup` and `caffeinate` command instead of running it, so it doesn't need root. `--simulate-lid` ignores the real lid and reads `open` / `closed` (or `o` / `c`) from stdin.

If the daemon is installed, stop it first. Two programs reading the same bot's updates conflict.

```bash
sudo launchctl bootout system/com.jamipuchi.gohome   # only if installed
cp gohome.example.json gohome.json && chmod 600 gohome.json   # fill in token, chat ID, SSID
python3 gohome.py --dry-run --simulate-lid open --config gohome.json --log-file gohome-dry-run.log
```

Then type `closed` and press Enter. The question arrives on your phone. Tap a button, or type `open` to cancel, and watch the log in the terminal. Press Ctrl-C to stop; the last line should be `Stopped; disablesleep set back to 0`. `gohome.json` is in `.gitignore`.

`python3 gohome.py --check --config gohome.json` checks the token, the chat ID and whether a webhook is set, without sending anything.

### Manual checklist on the real Mac

Run `tail -f /var/log/gohome.log` in a terminal, and use the Mac on battery with no external display.

1. **Before installing:** `pmset -g | grep -i sleep` shows `SleepDisabled 0` (or no such line).
2. **After installing:** the same command shows `SleepDisabled 1`, and the log shows `State START -> IDLE`.
3. **Going home:** close the lid at your desk. Within a few seconds the question arrives and the log shows `IDLE -> ASKING`. Tap 🏠. Turn on the iPhone hotspot. The log shows the join attempts, the message changes to "✅ Connected to hotspot", and on the iPhone the hotspot shows 1 connection. Open the lid: the lock screen asks for your password and the log shows `HOME -> IDLE`.
4. **Not going home:** close the lid and tap 🚫. The message changes to "😴 Sleeping (not going home)". Within about 10 s the Mac is asleep: the log stops, and `pmset -g log | grep -i "Entering Sleep"` shows it afterwards. Open the lid: the log shows `Woke up after sleeping … s` and `SLEEPING -> IDLE`, and `pmset -g | grep SleepDisabled` shows `1` again.
5. **Timeout:** close the lid and don't answer. After 3 minutes the message says "no answer within 180 s" and the Mac sleeps.
6. **Cancel:** close the lid, wait for the question, and open the lid before answering. The message changes to "↩️ Cancelled (lid opened)" and the Mac doesn't sleep.
7. **No network:** turn Wi-Fi off, then close the lid. After 3 minutes the log shows `SLEEPING (Telegram unreachable)` and the Mac sleeps.
8. **One question per close:** after step 4, leave the lid closed for 30+ minutes (Power Nap wakes are logged as `Woke up…`). No second question arrives.
9. **Maximum awake time:** set `"max_awake_minutes": 2`, restart the daemon, close the lid, and tap 🏠. After 2 minutes the Mac sleeps. Set it back afterwards.
10. **External display:** with a display connected, close the lid. No question arrives and the log shows `DOCKED`.
11. **Cleanup on stop:** `sudo launchctl bootout system/com.jamipuchi.gohome`, then `pmset -g | grep SleepDisabled` shows `0`. Load it again with `sudo launchctl bootstrap system /Library/LaunchDaemons/com.jamipuchi.gohome.plist`.
12. **Uninstall:** `sudo ./uninstall.sh`, then `pmset -g | grep SleepDisabled` shows `0`, and closing the lid sleeps the Mac immediately.

## License

MIT
