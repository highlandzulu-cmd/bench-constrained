#!/usr/bin/env python3
"""Side-by-side table of benchmark runs, with totals.

  python3 scripts/compare_runs.py [--compact] "Loop=results/1105-211130" "DeepSeek Harness=results/1106-004735"

Totals: "task time" is the sum of every task's own wall clock (what one-after-another would cost); "elapsed" is the real
time from the first pod being created to the last result being written (tasks run several at a time).
"""
import json
import statistics
import sys
from pathlib import Path


def load(run_dir: Path):
    rows = {}
    for f in sorted(run_dir.glob("*/*/*/result.json")):
        r = json.loads(f.read_text())
        r["_mtime"] = f.stat().st_mtime
        rows[r["task_id"]] = r
    return rows


def fmt_time(secs: float) -> str:
    m, s = divmod(round(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def summary(rows):
    done = list(rows.values())
    walls = [r["wall_clock_s"] for r in done if r.get("wall_clock_s") is not None]
    peaks = [r["peak_mem_mib"] for r in done if r.get("peak_mem_mib") is not None]
    start = min(r["_mtime"] - (r.get("elapsed_total_s") or 0) for r in done)
    end = max(r["_mtime"] for r in done)
    count = lambda s: sum(r["status"] == s for r in done)  # noqa: E731
    return {
        "tasks": len(done), "pass": count("pass"), "fail": count("fail"), "oom": count("oom"), "timeout": count("timeout"),
        "infra_error": count("infra_error"), "task_time": sum(walls), "elapsed": end - start,
        "median_time": statistics.median(walls) if walls else 0, "max_peak": max(peaks) if peaks else 0,
        "median_peak": statistics.median(peaks) if peaks else 0, "mean_peak": statistics.mean(peaks) if peaks else 0,
        "throttled": statistics.median([r.get("cpu_throttled_pct") or 0 for r in done]),
    }


def compact(runs):
    """One row per harness: the numbers to read first thing in the morning."""
    print("| Harness | Passed | OOM / timeout / infra | Median task time | Highest peak | Median peak | Total task time | Total elapsed |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for label, rows in runs:
        if not rows:
            print(f"| {label} | no results | | | | | | |")
            continue
        s = summary(rows)
        print(f"| {label} | {s['pass']} of {s['tasks']} | {s['oom']} / {s['timeout']} / {s['infra_error']} | {fmt_time(s['median_time'])} | "
              f"{s['max_peak']:g} MiB ({100 * s['max_peak'] / 512:.0f}%) | {s['median_peak']:g} MiB | {fmt_time(s['task_time'])} | {fmt_time(s['elapsed'])} |")


def main(argv):
    do_compact = "--compact" in argv
    argv = [a for a in argv if a != "--compact"]
    runs = []
    for a in argv:
        label, _, path = a.partition("=")
        runs.append((label, load(Path(path).expanduser())))
    if not runs:
        sys.exit(__doc__)
    if do_compact:
        return compact(runs)
    order = list(dict.fromkeys(t for _, rows in runs for t in rows))
    labels = [l for l, _ in runs]
    print("| Task | " + " | ".join(f"{l}: result | peak MiB | time" for l in labels) + " |")
    print("| --- | " + " | ".join("--- | ---: | ---:" for _ in labels) + " |")
    for t in order:
        cells = []
        for _, rows in runs:
            r = rows.get(t)
            cells.append("| - | - | - " if not r else f"| {r['status']} | {r.get('peak_mem_mib', '-')} | {fmt_time(r['wall_clock_s']) if r.get('wall_clock_s') is not None else '-'} ")
        print(f"| {t} " + "".join(cells) + "|")
    sums = [summary(rows) for _, rows in runs]
    def line(name, f):
        print(f"| **{name}** " + "".join(f"| {f(s)} | | " for s in sums) + "|")
    print("|  |" + "|".join(" | | " for _ in labels) + "|")
    line("Passed", lambda s: f"{s['pass']} of {s['tasks']}")
    line("Out of memory / timeout / infra errors", lambda s: f"{s['oom']} / {s['timeout']} / {s['infra_error']}")
    line("Median task time", lambda s: fmt_time(s["median_time"]))
    line("Highest peak memory (MiB)", lambda s: f"{s['max_peak']:g} ({100 * s['max_peak'] / 512:.0f}% of 512)")
    line("Median peak memory (MiB)", lambda s: f"{s['median_peak']:g}")
    line("Median CPU throttling", lambda s: f"{s['throttled']:g}% of periods")
    line("Total task time (sum of all tasks)", lambda s: f"**{fmt_time(s['task_time'])}**")
    line("Total elapsed (real, tasks in parallel)", lambda s: f"**{fmt_time(s['elapsed'])}**")


if __name__ == "__main__":
    main(sys.argv[1:])
