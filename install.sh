#!/bin/bash
# Installs gohome as a root LaunchDaemon.
#   sudo ./install.sh                 # first run creates /usr/local/etc/gohome.json to edit
#   sudo ./install.sh my-config.json  # install this config (replaces the current one)
set -euo pipefail

LABEL=com.jamipuchi.gohome
HERE="$(cd "$(dirname "$0")" && pwd)"
CONFIG=/usr/local/etc/gohome.json
SCRIPT=/usr/local/libexec/gohome.py
PLIST=/Library/LaunchDaemons/$LABEL.plist

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
    echo "/usr/bin/python3 needs the Command Line Tools. Run: xcode-select --install" >&2
    exit 1
fi
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
    echo "/usr/bin/python3 is older than 3.8" >&2
    exit 1
}

mkdir -p /usr/local/etc /usr/local/libexec
if [[ -n "${1:-}" ]]; then
    install -m 600 -o root -g wheel "$1" "$CONFIG"
    echo "Installed config $1 -> $CONFIG"
elif [[ ! -f $CONFIG ]]; then
    install -m 600 -o root -g wheel "$HERE/gohome.example.json" "$CONFIG"
    echo "Created $CONFIG from the example. Fill it in, then run this script again:"
    echo "  sudo nano $CONFIG"
    exit 1
fi
chown root:wheel "$CONFIG"
chmod 600 "$CONFIG"

install -m 644 -o root -g wheel "$HERE/gohome.py" "$SCRIPT"
echo "Checking config and Telegram access..."
/usr/bin/python3 -I "$SCRIPT" --check --config "$CONFIG"

touch /var/log/gohome.log
chmod 640 /var/log/gohome.log

if launchctl print "system/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "system/$LABEL" || true
    for _ in $(seq 20); do
        launchctl print "system/$LABEL" >/dev/null 2>&1 || break
        sleep 0.5
    done
fi
install -m 644 -o root -g wheel "$HERE/$LABEL.plist" "$PLIST"
launchctl bootstrap system "$PLIST"
sleep 2

echo
launchctl print "system/$LABEL" | grep -E '^\s*(state|pid) =' || true
pmset -g | grep -i SleepDisabled || true
echo
echo "Installed. Logs: /var/log/gohome.log (tail -f /var/log/gohome.log)"
