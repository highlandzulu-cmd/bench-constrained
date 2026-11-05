"""Pods must only ever land on the named nodes, at low priority, and a run refuses to start unscoped."""
import dataclasses
from types import SimpleNamespace as NS

from bench import cli
from bench.manifests import build_job
from bench.preflight import assess, node_use_from_api, parse_cpu, parse_mem_mib, scope_problem, worst_case


def pod_spec(profiles, settings, profile="tiny"):
    return build_job(run_id="r", task_id="BUG-01", profile=profiles[profile], repeat=1, settings=settings,
                     timeout_s=900)["spec"]["template"]["spec"]


def test_default_settings_pin_pods_to_the_named_nodes_and_a_low_priority(profiles, settings):
    spec = pod_spec(profiles, settings)
    terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    expr = terms[0]["matchExpressions"][0]
    assert expr["key"] == "kubernetes.io/hostname" and expr["operator"] == "In"
    assert expr["values"] == settings.allowed_nodes and len(settings.allowed_nodes) >= 1
    assert spec["priorityClassName"] == settings.priority_class == "test-low"


def test_unscoped_settings_are_refused(settings):
    open_ = dataclasses.replace(settings, allowed_nodes=[], node_selector={})
    assert "any node" in scope_problem(open_)
    assert scope_problem(settings) is None
    assert scope_problem(dataclasses.replace(settings, allowed_nodes=[], node_selector={"bench": "true"})) is None


def test_run_selftest_and_preflight_refuse_when_unscoped(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: dataclasses.replace(settings, allowed_nodes=[], node_selector={}))
    monkeypatch.setattr(cli, "_cluster", lambda a, s: (_ for _ in ()).throw(AssertionError("must not reach the cluster")))
    for argv in (["run"], ["selftest"], ["preflight"]):
        assert cli.main(argv) == 2
    assert "refusing to run" in capsys.readouterr().err


def test_quantity_parsing():
    assert parse_cpu("500m") == 0.5 and parse_cpu("128") == 128 and parse_cpu("") == 0
    assert round(parse_mem_mib("1056209624Ki") / 1024) == 1007 and parse_mem_mib("64Mi") == 64


def make_node(cpu="128", mem="1056209624Ki", taints=None):
    return NS(metadata=NS(name="n1"), spec=NS(taints=taints or []),
              status=NS(allocatable={"cpu": cpu, "memory": mem, "nvidia.com/gpu": "8"}))


def make_pod(ns, cpu="2", mem="8Gi", gpu="0"):
    return NS(metadata=NS(namespace=ns), spec=NS(containers=[NS(resources=NS(
        requests={"cpu": cpu, "memory": mem, "nvidia.com/gpu": gpu}))]))


def test_preflight_reports_other_teams_and_a_small_share(profiles, settings):
    use = node_use_from_api(make_node(), [make_pod("team-a", "32", "256Gi", "8"), make_pod("team-b", "16", "128Gi")])
    assert use.pods == 2 and use.req_gpu == 8 and use.by_namespace["team-a"] == 1
    cpu, mem = worst_case(settings, [profiles["tiny"]])
    assert (cpu, mem) == (settings.max_parallel * 1.0, settings.max_parallel * 512.0)
    lines = assess(use, cpu, mem)
    assert not any(level == "stop" for level, _ in lines)
    assert any("team-a:1" in m for _, m in lines)


def test_preflight_stops_when_the_node_is_full_and_warns_on_taints(profiles, settings):
    full = node_use_from_api(make_node(cpu="8", taints=[NS(key="nvidia.com/gpu", value="true", effect="NoSchedule")]),
                             [make_pod("team-a", "7.5", "8Gi")])
    cpu, mem = worst_case(settings, [profiles["baseline"]])          # 4 pods x 4 CPU
    levels = {level for level, _ in assess(full, cpu, mem)}
    assert "stop" in levels and "warn" in levels


def test_missing_node_is_a_stop():
    from bench.preflight import NodeUse
    assert assess(NodeUse(name="gone", found=False), 1, 1)[0][0] == "stop"


def test_preflight_checks_scratch_disk_too(settings):
    node = NS(metadata=NS(name="n"), spec=NS(taints=[]),
              status=NS(allocatable={"cpu": "128", "memory": "1056209624Ki", "ephemeral-storage": "20Gi"}))
    pods = [NS(metadata=NS(namespace="team-a"), spec=NS(containers=[NS(resources=NS(
        requests={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "10Gi"}))]))]
    use = node_use_from_api(node, pods)
    from bench.preflight import worst_case_disk_mib
    disk = worst_case_disk_mib(settings)                       # 4 pods x 4 GiB = 16 GiB, only 10 GiB free
    assert any(level == "stop" and "scratch disk" in m for level, m in assess(use, 1, 1, disk))


def test_in_pod_code_needs_no_pyyaml():
    """A stock python image has no PyYAML: the synthetic-workload path must import without it."""
    import subprocess, sys
    code = "import sys; sys.modules['yaml'] = None; import bench.podrun, bench.cgroup, bench.config, bench.tasks; print('ok')"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(__import__('pathlib').Path(__file__).resolve().parent.parent))
    assert out.stdout.strip() == "ok", out.stderr


def test_stock_image_job_mounts_the_runner_from_a_configmap(profiles, settings):
    j = build_job(run_id="r", task_id="SYNTH", profile=profiles["tiny"], repeat=1, settings=settings, timeout_s=60,
                  mode="verify", synth="alloc:64", image="python:3.12-slim", code_configmap="bench-code-r")
    pod = j["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["image"] == "python:3.12-slim"
    assert {"name": "code", "mountPath": "/opt/bench/bench", "readOnly": True} in c["volumeMounts"]
    assert {"name": "code", "configMap": {"name": "bench-code-r"}} in pod["volumes"]
    assert {"name": "PYTHONPATH", "value": "/opt/bench"} in c["env"]
    assert c["resources"]["limits"]["memory"] == "512Mi" and "affinity" in pod      # still limited and still pinned


def test_committed_settings_name_no_nodes_and_local_file_overrides(tmp_path):
    from bench.config import load_settings
    base = tmp_path / "settings.yaml"
    base.write_text((__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "settings.yaml").read_text())
    assert load_settings(base).allowed_nodes == []                       # what a fresh clone gets
    (tmp_path / "settings.local.yaml").write_text("allowed_nodes: [n1]\npriority_class: low\n")
    s = load_settings(base)
    assert s.allowed_nodes == ["n1"] and s.priority_class == "low"
