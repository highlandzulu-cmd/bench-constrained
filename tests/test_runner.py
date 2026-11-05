import json
from pathlib import Path

from bench.results import PodOutcome
from bench.runner import make_cells, run_cell, run_matrix


class FakeCluster:
    """Stands in for the API: a pod 'finishes' immediately with a scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes, self.created, self.deleted = outcomes, [], []

    def create_job(self, job):
        self.created.append(job)

    def wait(self, name, pending_timeout_s, max_wait_s):
        job = next(j for j in self.created if j["metadata"]["name"] == name)
        task = job["metadata"]["labels"]["bench/task"]
        return self.outcomes.get(task) or ok("pass")

    def delete_job(self, name):
        self.deleted.append(name)


def ok(status="pass"):
    return PodOutcome(exit_code=0, logs="BENCH_RESULT " + json.dumps(
        {"in_pod_status": status, "verifier_exit_code": 0 if status == "pass" else 1, "wall_clock_s": 5.0, "peak_mem_mib": 100.0}))


def test_matrix_runs_every_cell_writes_results_and_cleans_up(tmp_path, tasks, profiles, settings):
    cells = make_cells("r1", tasks[:3], [profiles["tiny"], profiles["starve_256"]], 2, "verify")
    assert len(cells) == 3 * 2 * 2
    cluster = FakeCluster({"BUG-01": PodOutcome(exit_code=137, terminated_reason="OOMKilled")})
    recs = run_matrix(cells, profiles, settings, cluster, tmp_path, max_parallel=3)
    assert all(r["attempts"] == 1 for r in recs)
    assert len(recs) == 12 and len(cluster.created) == 12 and len(cluster.deleted) == 12
    oom = [r for r in recs if r["task_id"] == "BUG-01"]
    assert all(r["status"] == "oom" for r in oom)
    f = tmp_path / "r1" / "BUG-01" / "tiny" / "1"
    assert (f / "result.json").exists() and (f / "job.yaml").exists()
    assert json.loads((f / "result.json").read_text())["status"] == "oom"


def test_api_failure_is_an_infra_error_not_a_crash(tmp_path, tasks, profiles, settings):
    class Boom(FakeCluster):
        def create_job(self, job):
            raise RuntimeError("quota exceeded")
    cell = make_cells("r2", tasks[:1], [profiles["tiny"]], 1, "verify")[0]
    rec = run_cell(cell, profiles["tiny"], settings, Boom({}), tmp_path)
    assert rec["status"] == "infra_error" and "quota exceeded" in rec["notes"][0]


def test_keep_flag_leaves_jobs(tmp_path, tasks, profiles, settings):
    cluster = FakeCluster({})
    cell = make_cells("r3", tasks[:1], [profiles["tiny"]], 1, "verify")[0]
    run_cell(cell, profiles["tiny"], settings, cluster, tmp_path, keep=True)
    assert cluster.deleted == []


def test_summary_excludes_infra_errors_from_the_pass_rate():
    from bench.report import summarize
    rows = summarize([
        {"profile": "tiny", "status": "pass", "wall_clock_s": 10, "peak_mem_mib": 200},
        {"profile": "tiny", "status": "oom", "wall_clock_s": 4, "peak_mem_mib": 512},
        {"profile": "tiny", "status": "infra_error"},
    ])
    r = rows[0]
    assert (r["cells"], r["counted"], r["pass_rate"], r["infra_error"]) == (3, 2, 50.0, 1)


def test_summary_prints_dashes_and_a_reason_when_nothing_could_be_counted():
    from bench.report import markdown, summarize
    text = markdown(summarize([{"profile": "tiny", "status": "infra_error"}]))
    assert "None" not in text and "| - | - | - |" in text and "infrastructure errors" in text


def test_a_runner_error_is_explained_in_the_result(tmp_path, tasks, profiles, settings):
    err = PodOutcome(exit_code=3, logs="BENCH_RESULT " + json.dumps(
        {"in_pod_status": "error", "error": "RuntimeError: LLM_API_KEY is not set (agent mode needs the model key)"}))
    rec = run_cell(make_cells("r9", tasks[:1], [profiles["tiny"]], 1, "agent")[0], profiles["tiny"], settings,
                   FakeCluster({"BUG-01": err}), tmp_path)
    assert rec["status"] == "infra_error" and "LLM_API_KEY is not set" in " ".join(rec["notes"])


def api_down(status="infra_error"):
    return PodOutcome(exit_code=0, logs="BENCH_RESULT " + json.dumps({
        "in_pod_status": "api_error", "agent_api_error": "loop: HTTP 502 Bad Gateway: The model is unavailable right now.",
        "verifier_exit_code": 101, "agent_exit_code": 1}))


def test_a_model_outage_is_an_infra_error_with_the_reason_not_a_task_failure(tmp_path, tasks, profiles, settings):
    from bench.results import classify
    rec = classify({"task_id": "BUG-01"}, api_down())
    assert rec["status"] == "infra_error" and "502 Bad Gateway" in " ".join(rec["notes"])
    assert "task was not attempted" in " ".join(rec["notes"])


def test_detect_api_error_only_for_a_nonzero_exit():
    from bench.podrun import detect_api_error
    tail = "loop: warning\nloop: HTTP 502 Bad Gateway: The model is unavailable right now.\n"
    assert "502" in detect_api_error(1, tail)
    assert detect_api_error(0, tail) is None and detect_api_error(None, tail) is None
    assert detect_api_error(1, "loop: edited 2 files\n") is None


class FlakyCluster(FakeCluster):
    """First N attempts hit a model outage, then the pod works."""
    def __init__(self, outages):
        super().__init__({})
        self.outages = outages

    def wait(self, name, pending_timeout_s, max_wait_s):
        if len(self.created) <= self.outages:
            return api_down()
        return ok("pass")


def test_infra_errors_are_retried_then_succeed(tmp_path, tasks, profiles, settings):
    from bench.runner import run_with_retries
    cell = make_cells("r10", tasks[:1], [profiles["tiny"]], 1, "agent")[0]
    cl = FlakyCluster(outages=2)
    rec = run_with_retries(cell, profiles["tiny"], settings, cl, tmp_path, sleep=lambda s: None)
    assert rec["status"] == "pass" and rec["attempts"] == 3 and len(cl.created) == 3
    assert len({j["metadata"]["name"] for j in cl.created}) == 3                 # each attempt is its own Job
    assert sum("infra_error, retried" in n for n in rec["notes"]) == 2          # the history is kept


def test_gives_up_after_the_retry_limit_and_reports_infra_error(tmp_path, tasks, profiles, settings):
    from bench.runner import run_with_retries
    cell = make_cells("r11", tasks[:1], [profiles["tiny"]], 1, "agent")[0]
    cl = FlakyCluster(outages=99)
    rec = run_with_retries(cell, profiles["tiny"], settings, cl, tmp_path, sleep=lambda s: None)
    assert rec["status"] == "infra_error" and rec["attempts"] == settings.infra_retries + 1


def test_oom_and_fail_and_timeout_are_never_retried(tmp_path, tasks, profiles, settings):
    from bench.runner import run_with_retries
    for outcome in (PodOutcome(exit_code=137, terminated_reason="OOMKilled"), ok("fail"), PodOutcome(pod_reason="DeadlineExceeded")):
        cell = make_cells("r12", tasks[:1], [profiles["tiny"]], 1, "agent")[0]
        cl = FakeCluster({"BUG-01": outcome})
        rec = run_with_retries(cell, profiles["tiny"], settings, cl, tmp_path, sleep=lambda s: None)
        assert rec["attempts"] == 1 and len(cl.created) == 1


def test_code_configmap_is_mounted_over_the_image_code(tmp_path, tasks, profiles, settings):
    from bench.runner import run_with_retries
    cell = make_cells("r13", tasks[:1], [profiles["tiny"]], 1, "agent")[0]
    cl = FakeCluster({})
    run_with_retries(cell, profiles["tiny"], settings, cl, tmp_path, code_configmap="bench-code-r13")
    pod = cl.created[0]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == settings.image                      # still the real image
    assert {"name": "code", "configMap": {"name": "bench-code-r13"}} in pod["volumes"]


def test_a_crash_in_our_own_runner_is_not_retried(tmp_path, tasks, profiles, settings):
    from bench.runner import run_with_retries
    crash = PodOutcome(exit_code=3, logs="BENCH_RESULT " + json.dumps({"in_pod_status": "error", "error": "Traceback ... FileNotFoundError"}))
    cell = make_cells("r14", tasks[:1], [profiles["tiny"]], 1, "agent")[0]
    cl = FakeCluster({"BUG-01": crash})
    rec = run_with_retries(cell, profiles["tiny"], settings, cl, tmp_path, sleep=lambda s: None)
    assert rec["status"] == "infra_error" and rec["attempts"] == 1 and len(cl.created) == 1


def test_config_paths_survive_a_symlinked_mount(tmp_path):
    """Kubernetes ConfigMap files are symlinks into a hidden folder; the runner must still find /opt/bench/config."""
    import os, subprocess, sys
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "bench"
    real, mount = tmp_path / "real", tmp_path / "opt" / "bench" / "bench"
    real.mkdir()
    mount.mkdir(parents=True)
    for f in ("__init__.py", "config.py"):
        (real / f).write_text((src / f).read_text())
        os.symlink(real / f, mount / f)
    out = subprocess.run([sys.executable, "-c", "import bench.config as c; print(c.ROOT)"],
                         env={**os.environ, "PYTHONPATH": str(tmp_path / "opt" / "bench")}, capture_output=True, text=True,
                         cwd=str(tmp_path))
    assert out.stdout.strip() == str(tmp_path / "opt" / "bench"), out.stderr
