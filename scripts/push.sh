#!/bin/sh
# Push source to the device. Exists because the Windows workstation driving this project
# has no `make` (Git Bash ships without it), so the Makefile targets silently did nothing
# and the device kept running stale code -- which cost a debugging round when a fixed
# file was "pushed" and the old bug persisted.
#
#   sh scripts/push.sh [user@host] [remote-dir]
set -e
ORIN=${1:-vanguard@192.168.55.1}
REMOTE=${2:-/opt/tutor}
cd "$(dirname "$0")/.."

ssh -o BatchMode=yes "$ORIN" "mkdir -p $REMOTE/eval $REMOTE/config $REMOTE/retrieval   $REMOTE/generation $REMOTE/ingest $REMOTE/api $REMOTE/ui $REMOTE/records $REMOTE/learn"

scp -q src/common/chunk_text.py   "$ORIN:$REMOTE/retrieval/"
scp -q src/common/chunk_text.py   "$ORIN:$REMOTE/ingest/"
# One retry policy, copied wherever a model is called. The device layout is flat, so a
# shared module has to be placed rather than imported across directories.
for d in retrieval generation learn; do
    scp -q src/common/http_retry.py "$ORIN:$REMOTE/$d/"
done
scp -q src/retrieval/*.py         "$ORIN:$REMOTE/retrieval/"
scp -q src/generation/*.py        "$ORIN:$REMOTE/generation/"
scp -q src/ingest/*.py            "$ORIN:$REMOTE/ingest/"
scp -q src/api/*.py               "$ORIN:$REMOTE/api/"
scp -q src/records/*.py           "$ORIN:$REMOTE/records/"
scp -q src/learn/*.py             "$ORIN:$REMOTE/learn/"
scp -q scripts/*.sh scripts/*.py  "$ORIN:$REMOTE/"
scp -q ui/index.html              "$ORIN:$REMOTE/ui/"
scp -q eval/run_eval.py eval/in_corpus.jsonl eval/out_of_corpus.jsonl "$ORIN:$REMOTE/eval/"
scp -q config/default.yaml        "$ORIN:$REMOTE/config/"

# Strip carriage returns from anything the device executes.
#
# .gitattributes forces LF *in git*, but a Windows working copy is checked out CRLF and
# scp copies it byte for byte. /bin/sh then fails on the very first line with
# "set: Illegal option -", which reads like a script bug rather than a line-ending
# problem and cost real debugging time. Doing it here, at the transfer boundary, means
# it holds regardless of any contributor's git config.
ssh -o BatchMode=yes "$ORIN"   "find $REMOTE -maxdepth 2 -type f \( -name '*.sh' -o -name '*.py' \) -print0 | xargs -0 -r sed -i 's/\r//'"

echo "pushed to $ORIN:$REMOTE (CR stripped from .sh/.py)"
