#!/bin/bash
# Removes gohome and restores normal lid-close sleep.
#   sudo ./uninstall.sh                # removes everything, including the config and logs
#   sudo ./uninstall.sh --keep-config  # keeps /usr/local/etc/gohome.json
set -uo pipefail

LABEL=com.jamipuchi.gohome

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

launchctl bootout "system/$LABEL" 2>/dev/null && echo "Stopped $LABEL"
rm -f "/Library/LaunchDaemons/$LABEL.plist" /usr/local/libexec/gohome.py
if [[ "${1:-}" == "--keep-config" ]]; then
    echo "Kept /usr/local/etc/gohome.json"
else
    rm -f /usr/local/etc/gohome.json
fi
rm -f /var/log/gohome.log /var/log/gohome.log.[0-9] /var/log/gohome.stderr.log

# the daemon does this on exit too; do it again in case it was killed with SIGKILL
pmset -a disablesleep 0
pmset -g | grep -i SleepDisabled
echo "Uninstalled. Closing the lid puts the Mac to sleep again."
