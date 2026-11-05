"""Turning what Kubernetes and the pod reported into one result record with a status.

Status rules:
  oom          any OOM kill happened (container killed, or a child process killed inside)
  timeout      the task or the Job deadline ran out
  pass / fail  the verifier's exit code, when the task ran to the end
  infra_error  the experiment itself failed (image pull, node lost, never scheduled, our own crash);
               reported separately and left out of the pass rate
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

RESULT_PREFIX = "BENCH_RESULT "
PROGRESS_PREFIX = "BENCH_PROGRESS "

INFRA_WAITING_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError",
                         "CreateContainerError", "ErrImageNeverPull"}


@dataclass
class PodOutcome:
    """What the cluster says happened, independent of the Kubernetes client library."""
    exit_code: int | None = None
    terminated_reason: str | None = None
    termination_message: str | None = None
    waiting_reason: str | None = None
    pod_reason: str | None = None            # e.g. Evicted, DeadlineExceeded
    pod_message: str | None = None
    job_failed_reason: str | None = None     # e.g. DeadlineExceeded, BackoffLimitExceeded
    pending_timed_out: bool = False
    queue_wait_s: float | None = None
    k8s_wall_s: float | None = None
    node: str | None = None
    logs: str = ""
    notes: list[str] = field(default_factory=list)


def parse_logs(logs: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(final result, last progress) found in a pod's log text."""
    final = progress = None
    for line in logs.splitlines():
        for prefix, slot in ((RESULT_PREFIX, "final"), (PROGRESS_PREFIX, "progress")):
            if line.startswith(prefix):
                try:
                    obj = json.loads(line[len(prefix):])
                except ValueError:
                    continue
                if slot == "final":
                    final = obj
                else:
                    progress = obj
    return final, progress


def _result_from_message(message: str | None) -> dict[str, Any] | None:
    if not message:
        return None
    try:
        obj = json.loads(message)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and "in_pod_status" in obj else None


def classify(cell: dict[str, Any], out: PodOutcome) -> dict[str, Any]:
    """Merge the in-pod result (if any) with the cluster's view into the final result record."""
    final, progress = parse_logs(out.logs)
    result = final or _result_from_message(out.termination_message)
    rec: dict[str, Any] = dict(cell)
    notes = list(out.notes)
    if result:
        rec.update({k: v for k, v in result.items() if k not in ("run_id", "profile", "repeat", "mode")})
    elif progress:
        rec["peak_mem_mib"] = progress.get("peak_mib")
        rec["peak_mem_source"] = "last progress line"
    rec["exit_code"] = out.exit_code
    rec["queue_wait_s"] = out.queue_wait_s
    rec["node"] = out.node
    if not result and out.k8s_wall_s is not None:
        rec["wall_clock_s"] = round(out.k8s_wall_s, 1)

    oom_inside = int((result or {}).get("oom_kills_inside") or 0)
    rec["oom_kills_container"] = 1 if (out.terminated_reason == "OOMKilled") else 0
    rec["oom_kills_inside"] = oom_inside

    if out.terminated_reason == "OOMKilled":
        status = "oom"
    elif oom_inside > 0:
        status = "oom"
        notes.append("a child process was OOM-killed inside the container")
    elif out.pod_reason == "DeadlineExceeded" or out.job_failed_reason == "DeadlineExceeded":
        status = "timeout"
    elif out.pod_reason == "Evicted":
        msg = (out.pod_message or "").lower()
        if "ephemeral" in msg:
            status, _ = "fail", notes.append("evicted: ephemeral-storage limit exceeded")
        else:
            status, _ = "infra_error", notes.append(f"evicted: {out.pod_message}")
    elif out.pending_timed_out:
        status, _ = "infra_error", notes.append("pod never started (still Pending): not scheduled or image not pulled")
    elif out.waiting_reason in INFRA_WAITING_REASONS:
        status, _ = "infra_error", notes.append(f"container could not start: {out.waiting_reason}")
    elif result:
        in_pod = result.get("in_pod_status")
        status = {"pass": "pass", "fail": "fail", "timeout": "timeout"}.get(in_pod, "infra_error")
        if in_pod == "api_error":
            notes.append("model API error, the task was not attempted: " + str(result.get("agent_api_error", "")))
        if in_pod == "error":
            notes.append("runner error: " + str(result.get("error", ""))[-300:])
    elif out.exit_code == 137:
        status = "oom"
        notes.append("exit 137 without a result and no OOMKilled reason; treated as a kill by the memory limit")
    else:
        status = "infra_error"
        notes.append(f"no result emitted (exit {out.exit_code}); the runner itself failed")

    rec["status"] = status
    rec["notes"] = notes
    return rec
