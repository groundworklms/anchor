#!/usr/bin/env python3
"""Measure cold boot to a USABLE tutor. Demo requirement: under 60 seconds.

"Usable" is deliberately strict. Reaching an ssh prompt is not usable, and neither is a
health endpoint returning ok while the model is still loading -- llama.cpp answers /health
before weights are resident. The clock stops only when a real doctrine question comes back
with a real answer, because that is what a judge will do.

    python3 boot_test.py --host 192.168.55.1 --user vanguard
"""
import argparse
import json
import subprocess
import time


class _Down:
    """Stand-in for a failed probe. A rebooting host times out rather than refusing,
    and an uncaught TimeoutExpired would abort the very measurement we are taking."""
    returncode = 255
    stdout = ""
    stderr = "unreachable"


def ssh(host, user, cmd, timeout=20):
    try:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={min(10, timeout)}", f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return _Down()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.55.1")
    ap.add_argument("--user", default="vanguard")
    ap.add_argument("--question", default="What is maneuver warfare?")
    ap.add_argument("--budget", type=float, default=60.0)
    ap.add_argument("--out", default="bench/boot_test.json")
    args = ap.parse_args()

    # Confirm the host is up first, so "went down" is observable rather than assumed.
    before = ssh(args.host, args.user, "cat /proc/uptime", timeout=10)
    if before.returncode != 0:
        print("host not reachable before reboot; aborting")
        return 1
    uptime_before = float(before.stdout.split()[0])
    print(f"host up {uptime_before:.0f}s before reboot; issuing reboot...")

    ssh(args.host, args.user, "sudo -n systemctl reboot", timeout=15)

    # WAIT FOR IT TO ACTUALLY GO DOWN before starting the clock. Without this the first
    # probe answers from the still-running system and the measurement is of a warm box.
    # That exact mistake produced a flattering 13.5s "cold boot" on a host whose uptime
    # was three hours.
    for _ in range(60):
        if ssh(args.host, args.user, "echo x", timeout=4).returncode != 0:
            break
        time.sleep(1)
    else:
        print("host never went down; reboot did not take. Check passwordless sudo.")
        return 1
    print("host is down; starting clock")
    t0 = time.time()

    marks = {}
    # The API is included deliberately. An earlier version of this test checked only the
    # three model servers and then asked its question through the CLI -- so it passed on a
    # boot that brought up inference and NO dashboard, which is the only thing a judge
    # would actually be looking at. A measurement can be true and still measure the wrong
    # thing.
    probe = (
        'curl -s -m 2 http://127.0.0.1:8081/health >/dev/null && '
        'curl -s -m 2 http://127.0.0.1:8082/health >/dev/null && '
        'curl -s -m 2 http://127.0.0.1:8080/health >/dev/null && '
        'curl -s -m 2 http://127.0.0.1:8000/api/health >/dev/null && echo ALLUP')

    while time.time() - t0 < args.budget + 120:
        el = time.time() - t0
        if "ssh" not in marks:
            r = ssh(args.host, args.user, "echo UP; cat /proc/uptime", timeout=8)
            if r.returncode == 0 and "UP" in r.stdout:
                marks["ssh"] = round(el, 1)
                print(f"  {marks['ssh']:6.1f}s  ssh reachable")
                # How long the kernel had been running when we first got in. Everything
                # before that is shutdown tail plus UEFI -- firmware we do not control,
                # and which does not happen at all on a true power-on.
                try:
                    up = float(r.stdout.split()[1])
                    marks["kernel_up_at_ssh"] = round(up, 1)
                    marks["pre_kernel"] = round(marks["ssh"] - up, 1)
                    print(f"  {'':6}   of which {marks['pre_kernel']:.1f}s was before the "
                          f"kernel started (shutdown tail + UEFI)")
                except (IndexError, ValueError):
                    pass
            else:
                continue
        if "services" not in marks:
            r = ssh(args.host, args.user, probe, timeout=10)
            if "ALLUP" in r.stdout:
                marks["services"] = round(time.time() - t0, 1)
                print(f"  {marks['services']:6.1f}s  all three model servers healthy")
            else:
                continue
        # The real test: a doctrine question answered THROUGH THE API, i.e. the same path
        # the kiosk UI uses, returning a cited answer.
        payload = json.dumps({"question": args.question})
        r = ssh(args.host, args.user,
                "curl -s -m 240 -X POST http://127.0.0.1:8000/api/ask "
                "-H 'Content-Type: application/json' "
                f"-d {json.dumps(payload)}",
                timeout=280)
        if r.returncode == 0 and '"citations"' in r.stdout:
            marks["usable"] = round(time.time() - t0, 1)
            print(f"  {marks['usable']:6.1f}s  answered a doctrine question  <-- USABLE")
            break

    ok = marks.get("usable") is not None and marks["usable"] <= args.budget
    print()
    print("=" * 58)
    print(f"  cold boot to usable : "
          f"{marks.get('usable', 'NOT REACHED')}s   budget {args.budget}s")
    print(f"  verdict             : {'PASS' if ok else 'FAIL'}")
    print("=" * 58)
    for k, v in marks.items():
        print(f"    {k:<18} {v}s")

    # The number that matters is power-on, not reboot: a fielded board is switched on, not
    # rebooted. Reporting both keeps the claim honest
    # in either direction rather than quietly picking the flattering one.
    if "pre_kernel" in marks and "usable" in marks:
        print()
        print(f"  measured from reboot          : {marks['usable']}s")
        print(f"  of which pre-kernel (firmware): {marks['pre_kernel']}s")
        print(f"  post-kernel (ours to fix)     : "
              f"{round(marks['usable'] - marks['pre_kernel'], 1)}s")

    try:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"marks": marks, "budget_s": args.budget, "pass": ok,
             "note": "clock starts when the host stops answering, so it includes the "
                     "shutdown tail; a true power-on excludes that"}, indent=2))
        print(f"\nwrote {args.out}")
    except OSError:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
