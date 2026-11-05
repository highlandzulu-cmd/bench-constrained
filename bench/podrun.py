"""In-pod entrypoint: prepare a work copy of the fixture, run the agent, run the verifier, report.

Runs inside the constrained container. Everything it measures is the container's own cgroup, so
the agent, the verifier and this script all count against the same memory and CPU limit ("whole" mode).

Result channel: one `BENCH_RESULT {json}` line on stdout (and /dev/termination-log when writable),
plus `BENCH_PROGRESS {json}` lines every few seconds so a container killed by the OOM killer still
leaves its last known peak behind.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import harnesses
from .cgroup import MIB, Cgroup, Sampler, snapshot
from .tasks import Task, get_task, load_tasks

IGNORE = shutil.ignore_patterns("node_modules", "target", "__pycache__", ".git")
TAIL_CHARS = 1500

# Loop prints these when the model endpoint, not the task, is the problem.
_API_ERROR = re.compile(r"HTTP (?:429|5\d\d)|Transport error|error decoding response|model is unavailable|"
                        r"timed out|connection (?:refused|reset)|Bad Gateway|Service Unavailable|Gateway Time-?out",
                        re.IGNORECASE)


_LAUNCH_ERROR = re.compile(r"command not found|No such file or directory|Cannot find module|ENOENT|EACCES|Permission denied|"
                           r"exec format error|not found in PATH|is not recognized", re.IGNORECASE)


def detect_launch_error(agent_exit: int | None, tail: str) -> str | None:
    """The first line explaining why the harness itself could not start (missing program, unwritable folder...)."""
    if agent_exit in (0, None):
        return None
    if agent_exit in (126, 127):
        return (tail.strip().splitlines() or [f"exit code {agent_exit}"])[-1][:300]
    for line in tail.splitlines():
        if _LAUNCH_ERROR.search(line):
            return line.strip()[:300]
    return None


def detect_api_error(agent_exit: int | None, tail: str) -> str | None:
    """The first line naming a model API failure, when the agent exited non-zero because of one."""
    if agent_exit in (0, None):
        return None
    for line in tail.splitlines():
        if _API_ERROR.search(line):
            return line.strip()[:300]
    return None


@dataclass
class PodConfig:
    task_id: str = ""
    synth: str = ""
    mode: str = "agent"                     # agent | verify
    run_id: str = ""
    profile: str = ""
    repeat: int = 1
    tasks_file: Path | None = None
    fixtures_dir: Path = Path("/opt/fixtures")
    work_dir: Path = Path("/work")
    solutions_dir: Path = Path("/opt/bench/solutions")
    cgroup_root: str = "/sys/fs/cgroup"
    verify_timeout_s: int = 300
    progress_every_s: float = 10.0
    agent: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "PodConfig":
        e = env if env is not None else os.environ
        agent = json.loads(e.get("BENCH_AGENT_JSON", "{}") or "{}")
        return cls(
            task_id=e.get("BENCH_TASK", ""),
            synth=e.get("BENCH_SYNTH", ""),
            mode=e.get("BENCH_MODE", "agent"),
            run_id=e.get("BENCH_RUN_ID", ""),
            profile=e.get("BENCH_PROFILE", ""),
            repeat=int(e.get("BENCH_REPEAT", "1")),
            tasks_file=Path(e["BENCH_TASKS_FILE"]) if e.get("BENCH_TASKS_FILE") else None,
            fixtures_dir=Path(e.get("BENCH_FIXTURES_DIR", "/opt/fixtures")),
            work_dir=Path(e.get("BENCH_WORK_DIR", "/work")),
            solutions_dir=Path(e.get("BENCH_SOLUTIONS_DIR", "/opt/bench/solutions")),
            cgroup_root=e.get("BENCH_CGROUP_ROOT", "/sys/fs/cgroup"),
            verify_timeout_s=int(e.get("BENCH_VERIFY_TIMEOUT_S", "300")),
            progress_every_s=float(e.get("BENCH_PROGRESS_EVERY_S", "10")),
            agent=agent,
        )


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:] if len(text) > TAIL_CHARS else text


def prepare_workdir(task: Task, cfg: PodConfig) -> Path:
    """Copy the fixture (without build output) into a fresh work folder."""
    work = cfg.work_dir / "task"
    if work.exists():
        shutil.rmtree(work)
    src = cfg.fixtures_dir / task.fixture
    shutil.copytree(src, work, ignore=IGNORE)
    if task.needs_node_modules:
        nm = src / "node_modules"
        if nm.is_dir():
            os.symlink(nm, work / "node_modules")
    return work


def _run(cmd: list[str] | str, cwd: Path, env: dict[str, str], timeout_s: int, log: Path,
         shell: bool = False) -> tuple[int | None, bool]:
    """Run to completion in its own process group; (exit code, timed_out). Output goes to `log`."""
    # Some tools (OpenCode, Kilo) trust $PWD over the real working directory, and a child process
    # started with cwd=... still inherits the parent's stale PWD. Set it so they work in the task folder.
    env = dict(env, PWD=str(cwd))
    with open(log, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=shell, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return None, True


def run_agent(task: Task, cfg: PodConfig, work: Path, env: dict[str, str]) -> tuple[int | None, bool, str]:
    home = cfg.work_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    spec = harnesses.launch(cfg.agent.get("harness", "loop"), task.prompt, work, home, cfg.agent, env)
    for d in spec.dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
    for path, content in spec.files.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)
    aenv = dict(env, HOME=str(home), **spec.env)
    log = cfg.work_dir / "agent.log"
    code, timed_out = _run(spec.cmd, work, aenv, task.timeout_s, log)
    return code, timed_out, _tail(log.read_text(errors="replace")) if log.exists() else ""


def apply_solution(task: Task, cfg: PodConfig, work: Path, env: dict[str, str]) -> bool:
    """verify mode: apply a known-good fix (tests, and checking the verifier inside the image)."""
    script = cfg.solutions_dir / f"{task.id}.sh"
    if not script.is_file():
        return False
    code, _ = _run(["sh", str(script)], work, env, 120, cfg.work_dir / "solution.log")
    return code == 0


def run_verifier(task: Task, cfg: PodConfig, work: Path, env: dict[str, str]) -> tuple[int | None, bool, str]:
    log = cfg.work_dir / "verify.log"
    code, timed_out = _run(["sh", "-c", task.verify_cmd], work, env, cfg.verify_timeout_s, log)
    return code, timed_out, _tail(log.read_text(errors="replace")) if log.exists() else ""


# ---- synthetic workloads, used to prove the limits and the measurements -------------------------

def _touch_alloc(mib: int) -> list[bytearray]:
    chunks: list[bytearray] = []
    for _ in range(max(1, mib // 16)):
        b = bytearray(16 * MIB)
        for i in range(0, len(b), 4096):
            b[i] = 1
        chunks.append(b)
    return chunks


def run_synth(spec: str, cfg: PodConfig) -> dict[str, Any]:
    """alloc:<MiB> | alloc_child:<MiB> | cpuwork:<units> | cpuburn:<processes>:<seconds> | sleep:<seconds>"""
    kind, _, arg = spec.partition(":")
    n = int(arg) if arg.isdigit() else 0
    notes: list[str] = []
    if kind == "alloc":
        held = _touch_alloc(n)
        time.sleep(2)
        notes.append(f"allocated {len(held) * 16} MiB")
    elif kind == "alloc_child":
        code = subprocess.run([sys.executable, "-c",
                               f"from bench.podrun import _touch_alloc;import time;_touch_alloc({n});time.sleep(2)"],
                              stdin=subprocess.DEVNULL, capture_output=True).returncode
        notes.append(f"child exit {code}")
    elif kind == "cpuwork":
        x = 0
        for _ in range(n * 1_000_000):
            x = (x * 31 + 7) & 0xFFFFFFFF
        notes.append(f"cpuwork {n}M iterations")
    elif kind == "cpuburn":
        # Several busy processes at once: under a 1 CPU limit their combined CPU time must stay near 1 s per second.
        procs, secs = (int(x) for x in arg.split(":"))
        burn = f"import time\nt=time.time()\nwhile time.time()-t<{secs}: pass"
        children = [subprocess.Popen([sys.executable, "-c", burn]) for _ in range(procs)]
        for c in children:
            c.wait()
        notes.append(f"{procs} busy processes for {secs}s")
    elif kind == "sleep":
        time.sleep(n)
        notes.append(f"slept {n}s")
    else:
        raise ValueError(f"unknown synthetic workload {spec!r}")
    return {"verifier_exit_code": 0, "notes": notes}


# ---- main flow -----------------------------------------------------------------------------------

def emit(prefix: str, payload: dict[str, Any]) -> None:
    print(f"{prefix} {json.dumps(payload, separators=(',', ':'))}", flush=True)


def _progress_loop(cg: Cgroup, sampler: Sampler, every_s: float, stop: threading.Event, t0: float) -> None:
    while not stop.wait(every_s):
        peak, _ = sampler.peak_bytes()
        emit("BENCH_PROGRESS", {"t": round(time.time() - t0, 1),
                                "mem_mib": round((cg.memory_current() or 0) / MIB, 1),
                                "peak_mib": round(peak / MIB, 1) if peak else None})


def run(cfg: PodConfig, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    t0 = time.time()
    cg = Cgroup(cfg.cgroup_root)
    sampler = Sampler(cg)
    sampler.start()
    stop = threading.Event()
    threading.Thread(target=_progress_loop, args=(cg, sampler, cfg.progress_every_s, stop, t0),
                     daemon=True).start()
    cfg.work_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {"run_id": cfg.run_id, "task_id": cfg.task_id or f"SYNTH:{cfg.synth}",
                              "profile": cfg.profile, "repeat": cfg.repeat, "mode": cfg.mode if not cfg.synth else "synth",
                              "agent_exit_code": None, "agent_timed_out": False, "verifier_exit_code": None,
                              "verifier_timed_out": False, "agent_s": 0.0, "verify_s": 0.0, "notes": []}
    try:
        if cfg.synth:
            out = run_synth(cfg.synth, cfg)
            result["verifier_exit_code"] = out["verifier_exit_code"]
            result["notes"] += out["notes"]
        else:
            task = get_task(load_tasks(cfg.tasks_file), cfg.task_id)
            work = prepare_workdir(task, cfg)
            if cfg.mode == "agent":
                need = harnesses.required_env(cfg.agent.get("harness", "loop"))
                if not env.get(need):
                    raise RuntimeError(f"{need} is not set (agent mode needs the model key)")
                ta = time.time()
                code, timed_out, tail = run_agent(task, cfg, work, env)
                result.update(agent_exit_code=code, agent_timed_out=timed_out, agent_s=round(time.time() - ta, 1),
                              agent_tail=tail)
                result["agent_api_error"] = detect_api_error(code, tail)
                result["agent_launch_error"] = None if result["agent_api_error"] else detect_launch_error(code, tail)
            elif cfg.mode == "verify":
                result["solution_applied"] = apply_solution(task, cfg, work, env)
            else:
                raise ValueError(f"unknown mode {cfg.mode!r}")
            if not result["agent_timed_out"]:
                tv = time.time()
                code, timed_out, tail = run_verifier(task, cfg, work, env)
                result.update(verifier_exit_code=code, verifier_timed_out=timed_out,
                              verify_s=round(time.time() - tv, 1), verify_tail=tail)
    except Exception:  # noqa: BLE001 - report, never crash silently
        result["error"] = _tail(traceback.format_exc())
    finally:
        stop.set()
        sampler.stop()
    result["wall_clock_s"] = round(time.time() - t0, 1)
    result.update(snapshot(cg, sampler))
    if result.get("error"):
        in_pod = "error"
    elif result.get("agent_api_error"):
        in_pod = "api_error"
    elif result.get("agent_launch_error"):
        in_pod = "launch_error"
    elif result["agent_timed_out"] or result["verifier_timed_out"]:
        in_pod = "timeout"
    elif result["verifier_exit_code"] == 0:
        in_pod = "pass"
    else:
        in_pod = "fail"
    result["in_pod_status"] = in_pod
    return result


def main() -> int:
    cfg = PodConfig.from_env()
    result = run(cfg)
    emit("BENCH_RESULT", result)
    compact = {k: v for k, v in result.items() if k not in ("agent_tail", "verify_tail", "error")}
    try:  # Kubernetes surfaces this file as the container's termination message (4 KiB max)
        Path("/dev/termination-log").write_text(json.dumps(compact, separators=(",", ":"))[:4000])
    except OSError:
        pass
    return 0 if result["in_pod_status"] != "error" else 3


if __name__ == "__main__":
    sys.exit(main())
