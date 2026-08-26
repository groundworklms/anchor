#!/bin/sh
# Install systemd units so the tutor is usable from a cold boot with no operator action.
#
# The brief requires "cold boot to usable in under 60 seconds". Before this, the three
# model servers were started by hand over ssh, so a cold boot reached a login prompt and
# nothing else -- the headline claim was false.
#
# Buffer sizes here are not defaults. The reranker was originally started with
# -b 2048 -ub 2048 and consumed 1,242 MB for a 304 MB model; the model caps at 512 tokens,
# so those buffers were pure waste. Right-sizing all three freed ~1 GB, which is the
# headroom the kiosk browser needs. Do not "tidy" these flags away.
#
#   sudo sh install_services.sh
set -e

BIN=/opt/tutor/llama.cpp/build/bin/llama-server
MODELS=/opt/tutor/models
USER_NAME=${SUDO_USER:-vanguard}

if [ ! -x "$BIN" ]; then
    echo "llama-server not found at $BIN" >&2
    exit 1
fi

unit() {
    name=$1; desc=$2; args=$3
    cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=${desc}
After=local-fs.target
# Deliberately NOT After=network-online.target. This device is air-gapped by design and
# must boot to a working state with no network present. Waiting on the network would add
# a timeout to every cold boot and make the 60-second requirement unmeetable unplugged.

[Service]
Type=simple
User=${USER_NAME}
ExecStart=${BIN} ${args}
Restart=on-failure
RestartSec=2
# Bound the blast radius if a model leaks: the board has 7.4 GiB total and three servers
# share it with Chromium.
MemoryMax=3500M
Nice=-5

[Install]
WantedBy=multi-user.target
EOF
}

unit tutor-embed "Doctrine Tutor - embedding server (bge-small-en-v1.5)" \
  "-m ${MODELS}/bge-small-en-v1.5-q8_0.gguf --embedding --pooling cls -ngl 999 --host 127.0.0.1 --port 8081 -c 512 -b 512 -ub 512"

unit tutor-rerank "Doctrine Tutor - reranker (bge-reranker-base)" \
  "-m ${MODELS}/bge-reranker-base-q8_0.gguf --rerank -ngl 999 --host 127.0.0.1 --port 8082 -c 512 -b 512 -ub 512"

unit tutor-gen "Doctrine Tutor - generator (gemma-4-E2B QAT Q4_0)" \
  "-m ${MODELS}/gemma-4-E2B_q4_0-it.gguf -ngl 999 --host 127.0.0.1 --port 8080 -c 4096 -b 1024 -ub 512 --parallel 1 --reasoning off --reasoning-budget 0"

# The API is a unit too. It was originally started by hand, which meant a cold boot
# brought up three model servers and NO dashboard. The boot test passed anyway, because
# it asked its question through the CLI rather than the UI a judge would be looking at --
# a measurement that was true and irrelevant.
cat > /etc/systemd/system/tutor-api.service <<EOF
[Unit]
Description=Doctrine Tutor - API and kiosk dashboard
After=local-fs.target tutor-gen.service tutor-embed.service tutor-rerank.service
Wants=tutor-gen.service tutor-embed.service tutor-rerank.service

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=/opt/tutor/api
ExecStart=/usr/bin/python3 /opt/tutor/api/main.py --db /opt/tutor/doctrine.sqlite --config /opt/tutor/config/default.yaml --ui /opt/tutor/ui --eval-json /opt/tutor/eval/last_run.json --records /opt/tutor/records.sqlite --gap-json /opt/tutor/gap_report.json --items /opt/tutor/learn_items.json
# Restart=always, not on-failure. A SIGTERM is a *clean* exit, so on-failure will not
# bring the dashboard back after anything kills it -- which is exactly what happened when
# a stray cleanup command killed the unit and it stayed dead for half an hour while an
# orphan held the port. On a demo device the dashboard should always come back.
Restart=always
RestartSec=3
# A start limit is not optional alongside Restart=always. Without one, a PERSISTENT
# failure -- port 8000 already held by an orphan, say -- becomes an infinite restart loop.
# That is not theoretical: this unit reached restart counter 386 while thrashing the
# device. Five attempts in a minute, then stop and stay stopped so the failure is visible
# rather than drowned in a loop.
StartLimitIntervalSec=60
StartLimitBurst=5
MemoryMax=800M

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/tutor-kiosk.service <<EOF
[Unit]
Description=Doctrine Tutor - full-screen kiosk on the attached monitor
After=tutor-api.service
Wants=tutor-api.service
# The console must be free before X can claim vt1.
Conflicts=getty@tty1.service
After=getty@tty1.service

[Service]
Type=simple
User=${USER_NAME}
# X on vt1 needs a real seat; without this the server starts and immediately exits.
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty
StandardOutput=journal
ExecStart=/bin/sh /opt/tutor/kiosk.sh
# The demo is the screen. If the kiosk dies the monitor goes black, which looks exactly
# like a crashed device to anyone watching, so it always comes back -- with the same
# start limit as the API so a persistent failure stays visible instead of thrashing.
Restart=always
RestartSec=3
StartLimitIntervalSec=60
StartLimitBurst=5

[Install]
WantedBy=graphical.target
EOF

# ---------------------------------------------------------------------------
# Close a passwordless root path.
#
# /etc/sudoers.d/91-tutor-reset grants NOPASSWD on `/bin/sh /opt/tutor/reset_demo.sh`,
# and that file is owned and WRITABLE by the vanguard user -- so anyone who can write it
# gets root without a password. scripts/push.sh overwrites it on every deploy, which
# means the workstation can push root-executed code across a USB cable.
#
# The fix is the standard one: root executes a copy that only root can write. The
# developer copy under /opt/tutor stays where push.sh expects it and is no longer the
# thing sudo runs.
RESET_SBIN=/usr/local/sbin/tutor-reset
if [ -f "${REMOTE:-/opt/tutor}/reset_demo.sh" ]; then
    install -o root -g root -m 0755 "${REMOTE:-/opt/tutor}/reset_demo.sh" "$RESET_SBIN"
    cat > /etc/sudoers.d/91-tutor-reset <<SUDOEOF
# Root runs the copy under /usr/local/sbin, which the service account cannot write.
# Pointing this at a user-writable path is passwordless root for anyone who owns it.
${USER_NAME} ALL=(ALL) NOPASSWD: ${RESET_SBIN}
SUDOEOF
    chmod 0440 /etc/sudoers.d/91-tutor-reset
    visudo -cf /etc/sudoers.d/91-tutor-reset >/dev/null && \
        echo "reset: root now runs $RESET_SBIN (was a user-writable script)" || \
        { echo "! sudoers syntax check FAILED; removing"; rm -f /etc/sudoers.d/91-tutor-reset; }
fi

systemctl daemon-reload
systemctl enable tutor-embed tutor-rerank tutor-gen tutor-api
echo "installed and enabled: tutor-embed tutor-rerank tutor-gen tutor-api"
echo
echo "reset is now: sudo tutor-reset      (NOT sudo sh /opt/tutor/reset_demo.sh)"
echo "  demo/script.md says the same. The old form no longer has a NOPASSWD rule."
echo "start now with: sudo systemctl start tutor-embed tutor-rerank tutor-gen tutor-api"
echo
echo "kiosk unit written but NOT enabled: it takes over tty1 and has never been"
echo "exercised against a real monitor. With a display attached, run:"
echo "    sudo systemctl start tutor-kiosk     # try it once"
echo "    sudo systemctl enable tutor-kiosk    # only after it works"
