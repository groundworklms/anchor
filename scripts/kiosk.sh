#!/bin/sh
# Bring up X with the kiosk as its only client.
#
# Waits for the API before starting X rather than after: starting X first means the
# monitor lights up showing a grey root window while the model loads, which reads as a
# hung machine to anyone watching. Waiting means the screen stays dark and then shows
# the tutor.
set -e
URL=${KIOSK_URL:-http://127.0.0.1:8000/}
DIR=$(cd "$(dirname "$0")" && pwd)
DEADLINE=$(( $(date +%s) + ${KIOSK_WAIT:-120} ))

while ! curl -fs -m 2 -o /dev/null "$URL"; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "kiosk: API never came up at $URL" >&2
        exit 1
    fi
    sleep 1
done

# -nocursor: there is no mouse at the demo table, and a stranded arrow in the middle of
# the screen is the kind of detail that makes an appliance look like a laptop.
exec xinit "$DIR/kiosk.py" "$URL" -- :0 vt1 -nolisten tcp -nocursor
