"""Running a matrix of cells in parallel and writing results/<run>/<task>/<profile>/<repeat>/."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from .config import Profile, Settings
from .manifests import build_job
from .results import PodOutcome, classify
from .tasks import Task


class ClusterLike(Protocol):
    def create_job(self, job: dict[str, Any]) -> None: ...
    def wait(self, name: str, pending_timeout_s: int, max_wait_s: int) -> PodOutcome: ...
    def delete_job(self, name: str) -> None: ...


def make_cells(run_id: str, tasks: list[Task], profiles: list[Profile], repeats: int, mode: str) -> list[dict[str, Any]]:
    return [{"run_id": run_id, "task_id": t.id, "profile": p.name, "repeat": r, "mode": mode,
             "backend": "k8s", "arch": p.arch, "cpu_limit": p.cpu_limit, "mem_limit_mib": p.memory_mib,
             "emulated": p.emulated, "timeout_s": t.timeout_s}
            for t in tasks for p in profiles for r in range(1, repeats + 1)]


def cell_dir(results_dir: Path, cell: dict[str, Any]) -> Path:
    return results_dir / cell["run_id"] / cell["task_id"].replace(":", "-") / cell["profile"] / str(cell["repeat"])


def run_cell(cell: dict[str, Any], profile: Profile, settings: Settings, cluster: ClusterLike,
             results_dir: Path, keep: bool = False, synth: str = "", deadline_s: int | None = None,
             image: str | None = None, code_configmap: str | None = None, attempt: int = 1, exec_cmd: str = "") -> dict[str, Any]:
    job = build_job(run_id=cell["run_id"], task_id=cell["task_id"], profile=profile, repeat=cell["repeat"],
                    settings=settings, timeout_s=cell["timeout_s"], mode=cell["mode"], synth=synth,
                    deadline_s=deadline_s, image=image, code_configmap=code_configmap, attempt=attempt, exec_cmd=exec_cmd)
    name = job["metadata"]["name"]
    d = cell_dir(results_dir, cell)
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.yaml").write_text(yaml.safe_dump(job, sort_keys=False))
    t0 = time.time()
    try:
        cluster.create_job(job)
        max_wait = job["spec"]["activeDeadlineSeconds"] + settings.pending_timeout_s + 120
        out = cluster.wait(name, settings.pending_timeout_s, max_wait)
        rec = classify(cell, out)
        (d / "logs.txt").write_text(out.logs)
    except Exception as e:  # noqa: BLE001 - an API failure is an infrastructure error, not a task result
        rec = dict(cell, status="infra_error", notes=[f"{type(e).__name__}: {e}"])
    rec["job"] = name
    rec["elapsed_total_s"] = round(time.time() - t0, 1)
    (d / "result.json").write_text(json.dumps(rec, indent=2, default=str))
    if not keep:
        try:
            cluster.delete_job(name)
        except Exception:  # noqa: BLE001
            pass
    return rec


def run_with_retries(cell: dict[str, Any], profile: Profile, settings: Settings, cluster: ClusterLike,
                     results_dir: Path, keep: bool = False, sleep: Callable[[float], None] = time.sleep,
                     code_configmap: str | None = None) -> dict[str, Any]:
    """Retry only infrastructure errors. An oom, fail or timeout is a result and is never retried."""
    history: list[str] = []
    for attempt in range(1, settings.infra_retries + 2):
        rec = run_cell(cell, profile, settings, cluster, results_dir, keep=keep, attempt=attempt,
                       code_configmap=code_configmap)
        rec["attempts"] = attempt
        if not rec.get("retryable") or attempt > settings.infra_retries:
            rec["notes"] = history + list(rec.get("notes", []))
            (cell_dir(results_dir, cell) / "result.json").write_text(json.dumps(rec, indent=2, default=str))
            return rec
        history.append(f"attempt {attempt}: infra_error, retried: " + " | ".join(str(n) for n in rec.get("notes", []))[:200])
        sleep(settings.infra_retry_wait_s)
    return rec


def run_matrix(cells: list[dict[str, Any]], profiles: dict[str, Profile], settings: Settings,
               cluster: ClusterLike, results_dir: Path, max_parallel: int | None = None, keep: bool = False,
               on_done: Callable[[dict[str, Any]], None] | None = None,
               code_configmap: str | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def one(cell: dict[str, Any]) -> dict[str, Any]:
        rec = run_with_retries(cell, profiles[cell["profile"]], settings, cluster, results_dir, keep=keep,
                               code_configmap=code_configmap)
        if on_done:
            on_done(rec)
        return rec

    with ThreadPoolExecutor(max_workers=max_parallel or settings.max_parallel) as pool:
        for rec in pool.map(one, cells):
            results.append(rec)
    return results
