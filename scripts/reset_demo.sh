#!/bin/sh
# One-key reset to a clean demo state. Demo requirement, and cheap insurance:
# a bad run on stage should cost fifteen seconds, not the pitch.
#
#   sudo sh reset_demo.sh          # reset and restart
#   sudo sh reset_demo.sh --keep   # reset runtime state, leave services running
#
# What it does NOT do: touch doctrine.sqlite. The corpus index is a static, hashed
# artifact; a reset that could alter it would invalidate the provenance chain the whole
# project rests on. Only runtime state is cleared.
set -e

TUTOR=/opt/tutor
KEEP_SERVICES=0
[ "$1" = "--keep" ] && KEEP_SERVICES=1

say() { printf "  %s\n" "$1"; }

echo "=== resetting demo state ==="

# Records are ARCHIVED, not deleted. A demo reset that silently destroys the learning
# records would undermine the exact claim the records exist to support.
if [ -f "$TUTOR/records.sqlite" ]; then
    STAMP=$(date +%Y%m%d-%H%M%S 2>/dev/null || echo manual)
    mkdir -p "$TUTOR/archive"
    mv "$TUTOR/records.sqlite" "$TUTOR/archive/records-$STAMP.sqlite"
    rm -f "$TUTOR/records.sqlite-wal" "$TUTOR/records.sqlite-shm"
    say "records archived -> archive/records-$STAMP.sqlite"
fi

rm -f "$TUTOR/gap_report.json" "$TUTOR/xapi_bundle.json"
say "gap report and export cleared"

if [ "$KEEP_SERVICES" -eq 0 ]; then
    # Order matters here, and getting it wrong cost real time.
    #
    # STOP the API first, then clear port 8000 unconditionally, then start. An earlier
    # version restarted the unit while a stale hand-started process still held the port:
    # the new instance could not bind, and because the unit carries Restart=always it
    # looped instead of failing visibly -- reaching restart counter 386 while thrashing
    # the board. Comparing the port holder against the unit's MainPID does not help,
    # because mid-activation that PID is the failing new process, not the squatter.
    systemctl stop tutor-api 2>/dev/null || true
    sleep 1

    SQUATTER=$(ss -lptnH "sport = :8000" 2>/dev/null | grep -oP "pid=\K[0-9]+" | head -1)
    if [ -n "$SQUATTER" ]; then
        kill -9 "$SQUATTER" 2>/dev/null || true
        say "cleared port 8000 (stale pid $SQUATTER)"
        sleep 1
    fi

    # Restart the model servers so the demo starts from clean memory. On a board where
    # 5.4 GiB of 7.4 is model weights, a leaked allocation from a previous session is a
    # real risk to the next one.
    systemctl reset-failed tutor-api 2>/dev/null || true
    systemctl restart tutor-embed tutor-rerank tutor-gen
    systemctl start tutor-api
    say "all four services restarting"
fi

echo "=== waiting for a usable tutor ==="
i=0
while [ $i -lt 120 ]; do
    if curl -s -m 2 http://127.0.0.1:8081/health 2>/dev/null | grep -q ok && \
       curl -s -m 2 http://127.0.0.1:8082/health 2>/dev/null | grep -q ok && \
       curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && \
       curl -s -m 2 http://127.0.0.1:8000/api/health 2>/dev/null | grep -q ok; then
        say "all services healthy after ${i}s"
        break
    fi
    i=$((i + 1))
    sleep 1
done

# "Healthy" is not "usable" -- llama.cpp answers /health while weights still load. The
# reset is not complete until a real question comes back.
say "verifying with a real question..."
OUT=$(curl -s -m 300 -X POST http://127.0.0.1:8000/api/ask \
      -H "Content-Type: application/json" \
      -d '{"question":"What is maneuver warfare?"}' 2>/dev/null || echo "")
case "$OUT" in
    *'"abstained":false'*) say "READY - answered a doctrine question" ;;
    *) echo "  !! NOT READY - the tutor did not answer. Check /tmp/api.log" ; exit 1 ;;
esac

echo "=== demo reset complete ==="
