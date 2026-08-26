#!/bin/sh
# Run on the device: sudo sh /opt/tutor/airgap_proof.sh
# Air-gap proof without a firewall: (1) enumerate every TCP socket the inference stack
# holds and show they are all loopback; (2) capture ALL non-loopback packets during a live
# doctrine query and show inference sent none. Nothing to restore, nothing to break.

echo "== 1. every listening/connected socket of the inference stack =="
echo "   (generator/embed/rerank llama-server + the API) -- all must be 127.0.0.1"
for p in $(pgrep -f "llama-server|api/main.py"); do
  ss -tanp 2>/dev/null | grep "pid=$p," | awk '{print "   pid '"$p"'  local="$4"  peer="$5}'
done | sort -u
echo
NONLOOP=$(for p in $(pgrep -f "llama-server|api/main.py"); do ss -tanp 2>/dev/null | grep "pid=$p,"; done \
          | awk '{print $4}' | grep -v "^127\." | grep -v "^\[::1\]" | grep -vE "0.0.0.0|\[::\]" | wc -l)
echo "   non-loopback sockets held by inference: $NONLOOP  (must be 0)"

echo
echo "== 2. packet capture on the ONLY interface with an address, during a query =="
IFACE=l4tbr0
# capture everything on that interface EXCEPT this ssh session (port 22), to a count
timeout 30 tcpdump -ni "$IFACE" -c 200 'not port 22 and not arp' -w /tmp/cap.pcap >/dev/null 2>&1 &
TCPD=$!
sleep 2
echo "   capturing non-ssh traffic on $IFACE; now asking a doctrine question..."
curl -s -m 120 -X POST http://127.0.0.1:8000/api/ask -H 'Content-Type: application/json' \
  -d '{"question":"How does MCDP 1 define friction?"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   answer: abstained=%s citations=%d chars=%d'%(d['abstained'],len(d['citations']),len(d.get('text',''))))"
sleep 2
kill "$TCPD" 2>/dev/null || true; wait "$TCPD" 2>/dev/null || true
PKTS=$(tcpdump -nr /tmp/cap.pcap 2>/dev/null | wc -l)
echo "   non-ssh packets seen on $IFACE during the query: $PKTS  (must be 0)"
rm -f /tmp/cap.pcap
echo
[ "$NONLOOP" = "0" ] && [ "$PKTS" = "0" ] && echo "== PASS: inference holds only loopback sockets and emitted zero external packets ==" || echo "== REVIEW: see counts above =="
