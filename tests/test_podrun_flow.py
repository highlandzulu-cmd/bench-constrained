"""The in-pod flow on this machine: untouched fixtures fail their verifier, reference fixes pass."""
import os
from pathlib import Path

import pytest

from bench.podrun import PodConfig, run

ROOT = Path(__file__).resolve().parent.parent
HAVE_SOLUTION = ["BUG-01", "AGENT-01", "FEAT-01", "TOOL-01", "REF-01", "WEB-BUG-01", "WEB-FEAT-01", "WEB-REF-01"]


def go(tmp_path, task, mode, solutions=True):
    return run(PodConfig(task_id=task, mode=mode, fixtures_dir=ROOT / "fixtures", work_dir=tmp_path,
                         solutions_dir=ROOT / "solutions", progress_every_s=999, cgroup_root=str(tmp_path / "nocg")))


@pytest.mark.parametrize("task", HAVE_SOLUTION)
def test_verifier_fails_untouched_and_passes_with_the_reference_fix(tmp_path, task):
    from bench import podrun
    # untouched: verify mode pointed at an empty solutions folder, so nothing is applied
    cfg = PodConfig(task_id=task, mode="verify", fixtures_dir=ROOT / "fixtures", work_dir=tmp_path / "u",
                    solutions_dir=tmp_path / "empty", progress_every_s=999, cgroup_root=str(tmp_path / "nocg"))
    r0 = podrun.run(cfg)
    assert r0["in_pod_status"] == "fail" and r0["solution_applied"] is False
    r1 = go(tmp_path / "s", task, "verify")
    assert r1["solution_applied"] is True and r1["in_pod_status"] == "pass", r1.get("verify_tail")


def test_workdir_is_a_copy_and_fixture_untouched(tmp_path):
    before = (ROOT / "fixtures/mini-calc-buggy/src/lib.rs").read_text()
    go(tmp_path, "BUG-01", "verify")
    assert (ROOT / "fixtures/mini-calc-buggy/src/lib.rs").read_text() == before


def test_agent_mode_without_key_is_a_runner_error_not_a_fail(tmp_path):
    cfg = PodConfig(task_id="BUG-01", mode="agent", fixtures_dir=ROOT / "fixtures", work_dir=tmp_path,
                    progress_every_s=999, cgroup_root=str(tmp_path / "nocg"))
    r = run(cfg, env={})
    assert r["in_pod_status"] == "error" and "LLM_API_KEY" in r["error"]


def test_synthetic_workloads_run_and_report(tmp_path):
    cfg = PodConfig(synth="alloc:32", work_dir=tmp_path, progress_every_s=999, cgroup_root=str(tmp_path / "nocg"))
    r = run(cfg)
    assert r["in_pod_status"] == "pass" and "allocated 32 MiB" in r["notes"]
    r = run(PodConfig(synth="cpuwork:1", work_dir=tmp_path, progress_every_s=999, cgroup_root=str(tmp_path / "nocg")))
    assert r["in_pod_status"] == "pass"


def test_cpuburn_synthetic_runs_several_processes(tmp_path):
    import time
    t0 = time.time()
    r = run(PodConfig(synth="cpuburn:3:1", work_dir=tmp_path, progress_every_s=999, cgroup_root=str(tmp_path / "nocg")))
    assert r["in_pod_status"] == "pass" and "3 busy processes for 1s" in r["notes"]
    assert 0.9 <= time.time() - t0 < 5          # the processes run side by side, not one after another


def test_cpuburn_actually_burns_cpu_on_this_machine(tmp_path):
    import resource
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    run(PodConfig(synth="cpuburn:2:1", work_dir=tmp_path, progress_every_s=999, cgroup_root=str(tmp_path / "nocg")))
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    used = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    assert used >= 1.6                          # 2 processes x 1 s of spinning


def test_child_processes_get_pwd_of_their_working_folder(tmp_path):
    """OpenCode-style tools read $PWD; it must be the task folder, not whatever the parent had."""
    from bench.podrun import _run
    work = tmp_path / "task"
    work.mkdir()
    log = tmp_path / "out.log"
    code, timed_out = _run(["python3", "-c", "import os; print(os.environ['PWD'])"], work,
                           {**os.environ, "PWD": "/some/other/stale/folder"}, 30, log)
    assert code == 0 and not timed_out and log.read_text().strip() == str(work)
