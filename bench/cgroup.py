"""Reading a container's own cgroup v2 counters (memory peak, OOM kills, CPU throttling).

Inside a Kubernetes container /sys/fs/cgroup is the container's own cgroup (v2), or holds one folder per
controller (v1, which some nodes of the Soket cluster still use).
Every reader returns None when the file is absent (cgroup v1, or running outside a container),
so the same code runs on a laptop for tests and the result says the data was unavailable.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

MIB = 1024 * 1024


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _read_kv(path: Path) -> dict[str, int] | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    out: dict[str, int] = {}
    for line in lines:
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


_V1_UNLIMITED = 1 << 60   # cgroup v1 reports "no limit" as a huge number


class Cgroup:
    """The container's own cgroup, either v2 (one unified folder) or v1 (one folder per controller).

    Both expose the same readers, so callers do not care which the node uses. On cgroup v1 the peak is
    memory.max_usage_in_bytes, the OOM counter is the oom_kill field of memory.oom_control, and
    throttling comes from the cpu controller's cpu.stat.
    """

    def __init__(self, root: str | Path = "/sys/fs/cgroup") -> None:
        self.root = Path(root)
        if (self.root / "memory.current").exists():
            self.version = 2
        elif (self.root / "memory" / "memory.usage_in_bytes").exists():
            self.version = 1
        else:
            self.version = 0
        self._mem = self.root / "memory"
        self._cpu = next((d for d in (self.root / "cpu", self.root / "cpu,cpuacct", self.root / "cpuacct")
                          if (d / "cpu.stat").exists() or (d / "cpuacct.usage").exists()), self.root / "cpu")

    @property
    def available(self) -> bool:
        return self.version != 0

    def memory_current(self) -> int | None:
        if self.version == 2:
            return _read_int(self.root / "memory.current")
        return _read_int(self._mem / "memory.usage_in_bytes") if self.version == 1 else None

    def memory_peak(self) -> int | None:
        """High-water mark: v2 memory.peak needs kernel 5.19+; v1 memory.max_usage_in_bytes always exists."""
        if self.version == 2:
            return _read_int(self.root / "memory.peak")
        return _read_int(self._mem / "memory.max_usage_in_bytes") if self.version == 1 else None

    def memory_max(self) -> int | None:
        if self.version == 2:
            return _read_int(self.root / "memory.max")
        value = _read_int(self._mem / "memory.limit_in_bytes") if self.version == 1 else None
        return None if value is None or value >= _V1_UNLIMITED else value

    def memory_events(self) -> dict[str, int]:
        """{"oom": n, "oom_kill": n}. On v1 only oom_kill exists (kernel 4.13+), reported for both."""
        if self.version == 2:
            return _read_kv(self.root / "memory.events") or {}
        if self.version == 1:
            kv = _read_kv(self._mem / "memory.oom_control") or {}
            kills = kv.get("oom_kill", 0)
            return {"oom": kills, "oom_kill": kills}
        return {}

    def cpu_stat(self) -> dict[str, int]:
        """nr_periods, nr_throttled, throttled_usec, usage_usec (v1 nanoseconds converted)."""
        if self.version == 2:
            return _read_kv(self.root / "cpu.stat") or {}
        if self.version != 1:
            return {}
        kv = _read_kv(self._cpu / "cpu.stat") or {}
        out = {"nr_periods": kv.get("nr_periods", 0), "nr_throttled": kv.get("nr_throttled", 0),
               "throttled_usec": kv.get("throttled_time", 0) // 1000}
        usage = _read_int(self._cpu / "cpuacct.usage")
        if usage is None:
            usage = _read_int(self.root / "cpuacct" / "cpuacct.usage")
        out["usage_usec"] = (usage or 0) // 1000
        return out


class Sampler(threading.Thread):
    """Polls memory.current so a peak is known even where memory.peak does not exist."""

    def __init__(self, cg: Cgroup, interval_s: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.cg = cg
        self.interval_s = interval_s
        self.max_current = 0
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            cur = self.cg.memory_current()
            if cur is not None and cur > self.max_current:
                self.max_current = cur
            time.sleep(self.interval_s)

    def stop(self) -> None:
        self._stop_evt.set()

    def peak_bytes(self) -> tuple[int | None, str]:
        """(peak bytes, source). Prefers the kernel's memory.peak, falls back to the sampled max."""
        peak = self.cg.memory_peak()
        if peak is not None:
            return max(peak, self.max_current), "memory.peak"
        if self.max_current:
            return self.max_current, "sampled"
        return None, "unavailable"


def snapshot(cg: Cgroup, sampler: Sampler | None = None) -> dict:
    """Counters at one moment, in the units result.json uses."""
    ev = cg.memory_events()
    cs = cg.cpu_stat()
    peak, source = sampler.peak_bytes() if sampler else (cg.memory_peak(), "memory.peak")
    nr_periods = cs.get("nr_periods", 0)
    nr_throttled = cs.get("nr_throttled", 0)
    mem_max = cg.memory_max()
    return {
        "cgroup_available": cg.available,
        "cgroup_version": cg.version,
        "peak_mem_mib": round(peak / MIB, 1) if peak is not None else None,
        "peak_mem_source": source,
        "mem_limit_mib": round(mem_max / MIB, 1) if mem_max is not None else None,
        "oom_events": ev.get("oom", 0),
        "oom_kills_inside": ev.get("oom_kill", 0),
        "nr_throttled": nr_throttled,
        "nr_periods": nr_periods,
        "cpu_throttled_s": round(cs.get("throttled_usec", 0) / 1e6, 3),
        "cpu_throttled_pct": round(100 * nr_throttled / nr_periods, 1) if nr_periods else 0.0,
        "cpu_usage_s": round(cs.get("usage_usec", 0) / 1e6, 3),
    }
