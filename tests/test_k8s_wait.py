"""Cluster.wait / _outcome against fake API objects (no cluster needed)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from bench.k8s import Cluster
from bench.results import classify

T0 = datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)


def terminated(code, reason=None, message=None, start=5, end=65):
    return NS(state=NS(terminated=NS(exit_code=code, reason=reason, message=message,
                                     started_at=T0 + timedelta(seconds=start), finished_at=T0 + timedelta(seconds=end)),
                       waiting=None))


def waiting(reason):
    return NS(state=NS(terminated=None, waiting=NS(reason=reason)))


def pod(phase, cs=None, reason=None, message=None, node="w1"):
    return NS(metadata=NS(name="p", creation_timestamp=T0), spec=NS(node_name=node),
              status=NS(phase=phase, reason=reason, message=message, container_statuses=[cs] if cs else []))


class FakeCore:
    def __init__(self, pods, logs=""):
        self.pods, self.logs, self.calls = list(pods), logs, 0

    def list_namespaced_pod(self, ns, label_selector):
        p = self.pods[min(self.calls, len(self.pods) - 1)]
        self.calls += 1
        return NS(items=[p] if p is not None else [])

    def read_namespaced_pod_log(self, name, ns, container):
        return self.logs


class FakeBatch:
    def __init__(self, failed_reason=None, succeeded=None):
        self.failed_reason, self.succeeded = failed_reason, succeeded

    def read_namespaced_job_status(self, name, ns):
        conds = [NS(type="Failed", reason=self.failed_reason)] if self.failed_reason else []
        return NS(status=NS(conditions=conds, succeeded=self.succeeded))


def cluster(core, batch=None):
    c = Cluster.__new__(Cluster)
    c.ns, c.core, c.batch, c.poll_s = "bench-constrained", core, batch or FakeBatch(), 0
    return c


CELL = {"run_id": "r", "task_id": "BUG-01", "profile": "tiny", "repeat": 1, "mode": "agent"}


def test_finished_pod_is_read_with_timings_and_logs():
    logs = 'BENCH_RESULT {"in_pod_status":"pass","verifier_exit_code":0,"wall_clock_s":58.0}'
    c = cluster(FakeCore([pod("Pending"), pod("Succeeded", terminated(0), node="worker-3")], logs))
    out = c.wait("j", pending_timeout_s=60, max_wait_s=60)
    assert out.exit_code == 0 and out.node == "worker-3" and out.queue_wait_s == 5 and out.k8s_wall_s == 60
    assert classify(CELL, out)["status"] == "pass"


def test_oom_killed_container():
    c = cluster(FakeCore([pod("Failed", terminated(137, "OOMKilled"))], 'BENCH_PROGRESS {"peak_mib":509.5}'))
    rec = classify(CELL, c.wait("j", 60, 60))
    assert rec["status"] == "oom" and rec["peak_mem_mib"] == 509.5


def test_pod_deadline_keeps_the_pod_and_is_a_timeout():
    c = cluster(FakeCore([pod("Failed", terminated(137), reason="DeadlineExceeded")]))
    assert classify(CELL, c.wait("j", 60, 60))["status"] == "timeout"


def test_image_pull_failure_ends_the_wait_early_as_infra_error():
    c = cluster(FakeCore([pod("Pending", waiting("ImagePullBackOff"))]))
    rec = classify(CELL, c.wait("j", pending_timeout_s=10_000, max_wait_s=10_000))
    assert rec["status"] == "infra_error" and "ImagePullBackOff" in rec["notes"][0]


def test_pod_stuck_pending_times_out_as_infra_error():
    c = cluster(FakeCore([pod("Pending")]))
    out = c.wait("j", pending_timeout_s=0, max_wait_s=10)
    assert out.pending_timed_out and classify(CELL, out)["status"] == "infra_error"


def test_pod_deleted_by_job_deadline():
    c = cluster(FakeCore([None]), FakeBatch(failed_reason="DeadlineExceeded"))
    assert classify(CELL, c.wait("j", 60, 60))["status"] == "timeout"


def test_refuses_to_create_outside_its_namespace():
    import pytest
    c = cluster(FakeCore([None]))
    with pytest.raises(ValueError):
        c.create_job({"metadata": {"namespace": "kube-system"}})
