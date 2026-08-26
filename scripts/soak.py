#!/usr/bin/env python3
"""8-hour soak: does the service hold memory flat under sustained use?

TRACKER B5. The robustness audit found `STATE["history"]` grew unbounded (now capped) and
warned that only a long run proves there is no slower leak. This drives the real serving
path -- one grounded query per interval -- and records RSS and available memory each time,
so a drift shows up as a trend rather than a surprise.

Deliberately gentle: one query at a time (never concurrent), matching the --parallel 1
single-learner reality, so the soak itself never causes the OOM it is watching for
(D-042/D-053).

    python3 soak.py --hours 8 --interval 180
"""
import argparse
import json
import subprocess
import time
import urllib.request

QUESTIONS = [
    "How does MCDP 1 define friction?",
    "What is the main effort?",
    "What are the principles of intelligence?",
    "What is maneuver warfare?",
    "What is a strategic corporal?",
    "How do Marines treat massive hemorrhage under fire?",
    "What is the OODA loop?",
    "What does MCDP 6 say about command and control?",
]


def rss_mb(port=8000):
    try:
        pid = subprocess.check_output(
            ["sh", "-c", f"ss -lptnH 'sport = :{port}' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2"],
            text=True).strip()
        if not pid:
            return None
        kb = subprocess.check_output(["sh", "-c", f"grep VmRSS /proc/{pid}/status | awk '{{print $2}}'"],
                                     text=True).strip()
        return round(int(kb) / 1024)
    except Exception:
        return None


def mem_avail_mb():
    try:
        out = subprocess.check_output(["free", "-m"], text=True).splitlines()[1].split()
        return int(out[6])
    except Exception:
        return None


def ask(q, timeout=120):
    body = json.dumps({"question": q}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/api/ask", data=body,
                                 headers={"Content-Type": "application/json", "Connection": "close"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return round(time.time() - t0, 2), d.get("abstained"), None
    except Exception as e:                                          # noqa: BLE001
        return round(time.time() - t0, 2), None, str(e)[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--out", default="/opt/tutor/soak.jsonl")
    args = ap.parse_args()

    deadline = time.time() + args.hours * 3600
    n, errors = 0, 0
    rss0 = rss_mb()
    print(f"soak start: api RSS {rss0} MB, mem avail {mem_avail_mb()} MB, "
          f"{args.hours}h at {args.interval}s", flush=True)
    with open(args.out, "w") as f:
        while time.time() < deadline:
            n += 1
            q = QUESTIONS[n % len(QUESTIONS)]
            lat, abst, err = ask(q)
            rec = {"n": n, "t": round(time.time()), "rss_mb": rss_mb(),
                   "avail_mb": mem_avail_mb(), "latency_s": lat,
                   "abstained": abst, "error": err}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            if err:
                errors += 1
            if n % 10 == 0 or err:
                print(f"  {n:4d}  rss={rec['rss_mb']}MB avail={rec['avail_mb']}MB "
                      f"lat={lat}s{'  ERR:'+err if err else ''}", flush=True)
            time.sleep(max(0, args.interval - lat))

    rss1 = rss_mb()
    drift = (rss1 - rss0) if (rss1 and rss0) else None
    print(f"\nsoak done: {n} queries, {errors} errors. "
          f"api RSS {rss0} -> {rss1} MB (drift {drift:+} MB). "
          f"Verdict: {'FLAT' if drift is not None and abs(drift) < 100 else 'REVIEW'}",
          flush=True)


if __name__ == "__main__":
    main()
