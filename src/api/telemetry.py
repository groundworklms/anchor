"""Device telemetry from tegrastats: watts, temperature, RAM, GPU load.

One long-lived tegrastats process feeds a shared latest-sample slot. Spawning tegrastats
per request would cost roughly a second each time and, on a board where memory is the
binding constraint, stacking processes to serve a 1 Hz dashboard is not free.

Every field is optional by design. tegrastats output varies across Jetson modules and L4T
releases, so the parser reports what it finds rather than asserting a schema — a dashboard
that crashes because a field moved is worse than one that shows a blank cell.
"""
import re
import subprocess
import threading
import time

RE_RAM = re.compile(r"RAM (\d+)/(\d+)MB")
RE_SWAP = re.compile(r"SWAP (\d+)/(\d+)MB")
RE_GR3D = re.compile(r"GR3D_FREQ (\d+)%")
RE_VDD_IN = re.compile(r"VDD_IN (\d+)mW(?:/(\d+)mW)?")
RE_CPU_TEMP = re.compile(r"cpu@([\d.]+)C")
RE_GPU_TEMP = re.compile(r"gpu@([\d.]+)C")
RE_TJ = re.compile(r"tj@([\d.]+)C")
RE_CPU = re.compile(r"CPU \[([^\]]+)\]")


def parse(line):
    s = {"raw_ts": time.time()}
    if (m := RE_RAM.search(line)):
        s["ram_used_mb"], s["ram_total_mb"] = int(m.group(1)), int(m.group(2))
    if (m := RE_SWAP.search(line)):
        s["swap_used_mb"] = int(m.group(1))
    if (m := RE_GR3D.search(line)):
        s["gpu_util_pct"] = int(m.group(1))
    if (m := RE_VDD_IN.search(line)):
        s["power_w"] = round(int(m.group(1)) / 1000.0, 2)
        if m.group(2):
            s["power_avg_w"] = round(int(m.group(2)) / 1000.0, 2)
    if (m := RE_CPU_TEMP.search(line)):
        s["cpu_temp_c"] = float(m.group(1))
    if (m := RE_GPU_TEMP.search(line)):
        s["gpu_temp_c"] = float(m.group(1))
    if (m := RE_TJ.search(line)):
        s["tj_temp_c"] = float(m.group(1))
    if (m := RE_CPU.search(line)):
        loads = [int(p.split("%")[0]) for p in m.group(1).split(",")
                 if "%" in p and p.split("%")[0].strip().isdigit()]
        if loads:
            s["cpu_util_pct"] = round(sum(loads) / len(loads))
    return s


class TelemetryReader(threading.Thread):
    def __init__(self, interval_ms=1000):
        super().__init__(daemon=True)
        self.interval_ms = interval_ms
        self._latest = {}
        self._lock = threading.Lock()
        self._proc = None
        self.error = None

    def run(self):
        try:
            self._proc = subprocess.Popen(
                ["tegrastats", "--interval", str(self.interval_ms)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            self.error = f"tegrastats unavailable: {e}"
            return
        for line in self._proc.stdout:
            s = parse(line)
            if len(s) > 1:
                with self._lock:
                    self._latest = s

    def latest(self):
        with self._lock:
            out = dict(self._latest)
        if self.error:
            out["error"] = self.error
        out["stale"] = (not out) or (time.time() - out.get("raw_ts", 0) > 5)
        return out

    def stop(self):
        if self._proc:
            self._proc.terminate()
        subprocess.run(["tegrastats", "--stop"], capture_output=True)


_reader = None


def get_reader():
    global _reader
    if _reader is None:
        _reader = TelemetryReader()
        _reader.start()
    return _reader


def power_mode():
    try:
        out = subprocess.run(["nvpmodel", "-q"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            if "NV Power Mode" in line:
                return line.split(":")[-1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


if __name__ == "__main__":
    import json
    r = get_reader()
    time.sleep(2.5)
    print(json.dumps({"telemetry": r.latest(), "power_mode": power_mode()}, indent=2))
