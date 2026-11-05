"""A plain summary of one run: per profile, pass rate and where the failures came from."""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


def load_results(results_dir: Path, run_id: str) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((results_dir / run_id).glob("*/*/*/result.json"))]


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by.setdefault(r["profile"], []).append(r)
    rows = []
    for profile, rs in by.items():
        counted = [r for r in rs if r["status"] != "infra_error"]       # infra errors are not in the pass rate
        walls = [r["wall_clock_s"] for r in counted if r.get("wall_clock_s") is not None]
        rows.append({
            "profile": profile, "cells": len(rs), "counted": len(counted),
            "pass": sum(r["status"] == "pass" for r in counted),
            "fail": sum(r["status"] == "fail" for r in counted),
            "oom": sum(r["status"] == "oom" for r in counted),
            "timeout": sum(r["status"] == "timeout" for r in counted),
            "infra_error": len(rs) - len(counted),
            "pass_rate": round(100 * sum(r["status"] == "pass" for r in counted) / len(counted), 1) if counted else None,
            "median_wall_s": round(statistics.median(walls), 1) if walls else None,
            "max_peak_mib": max((r["peak_mem_mib"] for r in counted if r.get("peak_mem_mib")), default=None),
        })
    return rows


def _cell(v: Any) -> str:
    return "-" if v is None else str(v)


def markdown(rows: list[dict[str, Any]]) -> str:
    head = "| profile | cells | pass | fail | oom | timeout | infra_error | pass rate % | median wall s | max peak MiB |\n"
    head += "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    body = "\n".join(
        f"| {r['profile']} | {r['cells']} | {r['pass']} | {r['fail']} | {r['oom']} | {r['timeout']} | "
        f"{r['infra_error']} | {_cell(r['pass_rate'])} | {_cell(r['median_wall_s'])} | {_cell(r['max_peak_mib'])} |" for r in rows)
    note = [f"\nNo result for profile {r['profile']}: all {r['cells']} cells were infrastructure errors, so there is "
            f"nothing to count. The reason is printed above and saved in each cell's result.json."
            for r in rows if r["counted"] == 0]
    return head + body + "".join(note)
