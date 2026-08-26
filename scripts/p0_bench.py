#!/usr/bin/env python3
"""P0 benchmark sweep. Runs ON the Jetson Orin Nano 8GB. Needs root for nvpmodel.

Produces the committed benchmark table: tokens/sec, load time, peak RAM, and watts,
for each model at each available power mode.

Sweeping power modes rather than reporting a single number is deliberate: for a project
judged on deployability, "N tok/s at 7W" is a stronger claim than a bigger number at
unstated wattage.

    sudo python3 p0_bench.py --out bench.json
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

LLAMA_BENCH = Path("/opt/tutor/llama.cpp/build/bin/llama-bench")
NVPMODEL_CONF = Path("/etc/nvpmodel.conf")

# tegrastats lines look like:
#   RAM 3456/7620MB ... CPU [12%@1510,...] ... GPU 99%@624 ... VDD_IN 7123mW/6800mW ...
RE_RAM = re.compile(r"RAM (\d+)/(\d+)MB")
RE_VDD_IN = re.compile(r"VDD_IN (\d+)mW")
RE_TEMP_GPU = re.compile(r"(?:gpu|GPU)@([\d.]+)C")
RE_TEMP_CPU = re.compile(r"(?:cpu|CPU)@([\d.]+)C")


class TegraSampler(threading.Thread):
    """Samples tegrastats in the background for the duration of one benchmark run."""

    def __init__(self, interval_ms=500):
        super().__init__(daemon=True)
        self.interval_ms = interval_ms
        self.samples = []
        self._stop = threading.Event()
        self._proc = None

    def run(self):
        self._proc = subprocess.Popen(
            ["tegrastats", "--interval", str(self.interval_ms)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            s = {}
            if (m := RE_RAM.search(line)):
                s["ram_used_mb"] = int(m.group(1))
                s["ram_total_mb"] = int(m.group(2))
            if (m := RE_VDD_IN.search(line)):
                s["power_mw"] = int(m.group(1))
            if (m := RE_TEMP_GPU.search(line)):
                s["gpu_temp_c"] = float(m.group(1))
            if (m := RE_TEMP_CPU.search(line)):
                s["cpu_temp_c"] = float(m.group(1))
            if s:
                self.samples.append(s)

    def stop(self):
        self._stop.set()
        if self._proc:
            self._proc.terminate()
        subprocess.run(["tegrastats", "--stop"], capture_output=True)

    def summary(self):
        def agg(key, fn):
            vals = [s[key] for s in self.samples if key in s]
            return round(fn(vals), 1) if vals else None

        return {
            "samples": len(self.samples),
            "peak_ram_mb": agg("ram_used_mb", max),
            "mean_ram_mb": agg("ram_used_mb", lambda v: sum(v) / len(v)),
            "total_ram_mb": agg("ram_total_mb", max),
            "peak_power_w": (p / 1000 if (p := agg("power_mw", max)) else None),
            "mean_power_w": (p / 1000 if (p := agg("power_mw", lambda v: sum(v) / len(v))) else None),
            "peak_gpu_temp_c": agg("gpu_temp_c", max),
            "peak_cpu_temp_c": agg("cpu_temp_c", max),
        }


def available_power_modes():
    """Parse the ACTIVE nvpmodel conf. Reflects whether Super is enabled."""
    modes = []
    text = NVPMODEL_CONF.read_text(errors="replace")
    for m in re.finditer(r"< POWER_MODEL ID=(\d+) NAME=(\S+) >", text):
        modes.append({"id": int(m.group(1)), "name": m.group(2)})
    return modes


def set_power_mode(mode_id):
    r = subprocess.run(["nvpmodel", "-m", str(mode_id)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! nvpmodel -m {mode_id} failed: {r.stderr.strip()}", file=sys.stderr)
        return False
    # Clocks need a moment to settle before the numbers mean anything.
    time.sleep(5)
    return True


def current_power_mode():
    r = subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True)
    name = mid = None
    for line in r.stdout.splitlines():
        if "NV Power Mode" in line:
            name = line.split(":")[-1].strip()
        elif line.strip().isdigit():
            mid = int(line.strip())
    return {"id": mid, "name": name}


def run_llama_bench(gguf, n_gpu_layers=999, prompt_tokens=512, gen_tokens=128, reps=3):
    """Run llama-bench and parse prompt-processing and token-generation throughput."""
    cmd = [
        str(LLAMA_BENCH), "-m", gguf,
        "-ngl", str(n_gpu_layers),
        "-p", str(prompt_tokens),
        "-n", str(gen_tokens),
        "-r", str(reps),
        "-o", "json",
    ]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    elapsed = time.time() - t0
    if r.returncode != 0:
        return {"error": r.stderr.strip()[-2000:], "wall_s": round(elapsed, 1)}
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": "could not parse llama-bench json", "raw": r.stdout[-2000:]}

    out = {"wall_s": round(elapsed, 1), "runs": []}
    for row in rows:
        out["runs"].append({
            "n_prompt": row.get("n_prompt"),
            "n_gen": row.get("n_gen"),
            "tokens_per_second": round(float(row.get("avg_ts", 0)), 2),
            "stddev_ts": round(float(row.get("stddev_ts", 0)), 2),
        })
    for run in out["runs"]:
        if run["n_gen"]:
            out["tg_tokens_per_second"] = run["tokens_per_second"]
        elif run["n_prompt"]:
            out["pp_tokens_per_second"] = run["tokens_per_second"]
    return out


def measure_load_time(gguf):
    """Cold-ish load time: how long until the model is resident and ready."""
    subprocess.run(["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
                   capture_output=True)
    t0 = time.time()
    r = subprocess.run(
        [str(LLAMA_BENCH), "-m", gguf, "-ngl", "999", "-p", "1", "-n", "1", "-r", "1"],
        capture_output=True, text=True, timeout=900,
    )
    return {
        "load_plus_minimal_run_s": round(time.time() - t0, 1),
        "ok": r.returncode == 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", nargs="+", default=[
        "/opt/tutor/models/gemma-4-E2B-it-qat-Q4_0.gguf",
        "/opt/tutor/models/gemma-4-E4B-it-qat-Q4_0.gguf",
    ])
    ap.add_argument("--modes", nargs="*", type=int,
                    help="power mode IDs to sweep; default = all available")
    args = ap.parse_args()

    if not LLAMA_BENCH.exists():
        sys.exit(f"llama-bench not found at {LLAMA_BENCH} — run `make build-llama` first")

    modes = available_power_modes()
    if args.modes:
        modes = [m for m in modes if m["id"] in args.modes]

    original = current_power_mode()
    print(f"Power modes available: {[m['name'] for m in modes]}")
    print(f"Currently: {original}")
    print(f"nvpmodel conf: {NVPMODEL_CONF.resolve()}")

    results = {
        "meta": {
            "device": "Jetson Orin Nano 8GB (p3767-0003)",
            "nvpmodel_conf": str(NVPMODEL_CONF.resolve()),
            "super_profile": "super" in str(NVPMODEL_CONF.resolve()),
            "timestamp_note": "device RTC is unreliable (see DECISIONS.md D-010); "
                              "stamp this table from the host, not the device",
        },
        "power_modes": modes,
        "results": [],
    }

    for mode in modes:
        print(f"\n=== power mode {mode['id']} ({mode['name']}) ===")
        if not set_power_mode(mode["id"]):
            continue
        for gguf in args.models:
            if not Path(gguf).exists():
                print(f"  - skip (missing): {gguf}")
                results["results"].append({
                    "mode": mode, "model": gguf, "skipped": "file not found"})
                continue

            name = Path(gguf).name
            print(f"  - {name}: load time ...", flush=True)
            load = measure_load_time(gguf)

            print(f"  - {name}: throughput ...", flush=True)
            sampler = TegraSampler()
            sampler.start()
            time.sleep(1)
            bench = run_llama_bench(gguf)
            time.sleep(1)
            sampler.stop()
            sampler.join(timeout=5)

            entry = {
                "mode": mode,
                "model": name,
                "gguf_path": gguf,
                "load": load,
                "bench": bench,
                "telemetry": sampler.summary(),
            }
            results["results"].append(entry)
            tg = bench.get("tg_tokens_per_second")
            tel = entry["telemetry"]
            print(f"    tg={tg} tok/s  peak_ram={tel['peak_ram_mb']}MB  "
                  f"mean_power={tel['mean_power_w']}W  peak_gpu={tel['peak_gpu_temp_c']}C")

    # Leave the box how we found it.
    if original.get("id") is not None:
        set_power_mode(original["id"])
        print(f"\nrestored power mode {original}")

    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
