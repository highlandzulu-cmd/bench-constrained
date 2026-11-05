import json

from bench.results import PodOutcome, classify, parse_logs

CELL = {"run_id": "r", "task_id": "BUG-01", "profile": "tiny", "repeat": 1, "mode": "agent"}


def logs(result=None, progress=None):
    lines = ["noise"]
    if progress:
        lines.append("BENCH_PROGRESS " + json.dumps(progress))
    if result:
        lines.append("BENCH_RESULT " + json.dumps(result))
    return "\n".join(lines)


def result(status, **kw):
    base = {"in_pod_status": status, "verifier_exit_code": 0 if status == "pass" else 1, "wall_clock_s": 12.0,
            "peak_mem_mib": 300.0, "oom_kills_inside": 0}
    base.update(kw)
    return base


def test_pass_and_fail():
    assert classify(CELL, PodOutcome(exit_code=0, logs=logs(result("pass"))))["status"] == "pass"
    assert classify(CELL, PodOutcome(exit_code=0, logs=logs(result("fail"))))["status"] == "fail"


def test_container_oom_wins_and_keeps_last_peak():
    out = PodOutcome(exit_code=137, terminated_reason="OOMKilled",
                     logs=logs(progress={"peak_mib": 511.0}))
    rec = classify(CELL, out)
    assert rec["status"] == "oom" and rec["oom_kills_container"] == 1 and rec["peak_mem_mib"] == 511.0


def test_child_oom_inside_counts_even_when_tests_passed():
    rec = classify(CELL, PodOutcome(exit_code=0, logs=logs(result("pass", oom_kills_inside=2))))
    assert rec["status"] == "oom" and rec["oom_kills_inside"] == 2 and rec["oom_kills_container"] == 0


def test_timeouts():
    assert classify(CELL, PodOutcome(pod_reason="DeadlineExceeded"))["status"] == "timeout"
    assert classify(CELL, PodOutcome(job_failed_reason="DeadlineExceeded"))["status"] == "timeout"
    assert classify(CELL, PodOutcome(exit_code=0, logs=logs(result("timeout"))))["status"] == "timeout"


def test_infrastructure_errors_are_not_task_results():
    assert classify(CELL, PodOutcome(waiting_reason="ImagePullBackOff"))["status"] == "infra_error"
    assert classify(CELL, PodOutcome(pending_timed_out=True))["status"] == "infra_error"
    assert classify(CELL, PodOutcome(pod_reason="Evicted", pod_message="node had condition: DiskPressure"))["status"] == "infra_error"
    assert classify(CELL, PodOutcome(exit_code=1, logs="Traceback ..."))["status"] == "infra_error"


def test_ephemeral_storage_eviction_is_a_task_failure():
    rec = classify(CELL, PodOutcome(pod_reason="Evicted", pod_message="Pod ephemeral local storage usage exceeds the total limit"))
    assert rec["status"] == "fail"


def test_exit_137_without_reason_is_oom():
    assert classify(CELL, PodOutcome(exit_code=137))["status"] == "oom"


def test_result_from_termination_message_when_logs_missing():
    msg = json.dumps(result("pass"))
    assert classify(CELL, PodOutcome(exit_code=0, termination_message=msg))["status"] == "pass"


def test_parse_logs_ignores_garbage_and_takes_the_last():
    final, prog = parse_logs("BENCH_RESULT {bad\nBENCH_PROGRESS {\"peak_mib\":1}\nBENCH_PROGRESS {\"peak_mib\":2}")
    assert final is None and prog == {"peak_mib": 2}
