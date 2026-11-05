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
        return self.outcomes.get(task, PodOutcome(exit_code=0, logs=""))

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
