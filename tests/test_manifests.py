from bench.manifests import build_job, guaranteed_qos, job_name, labels


def job(profiles, settings, profile="tiny", **kw):
    return build_job(run_id="r1", task_id="BUG-01", profile=profiles[profile], repeat=1, settings=settings,
                     timeout_s=900, **kw)


def test_tiny_is_exactly_512mi_and_one_cpu_guaranteed(profiles, settings):
    j = job(profiles, settings)
    c = j["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["memory"] == "512Mi" and c["resources"]["limits"]["cpu"] == "1"
    assert c["resources"]["requests"] == c["resources"]["limits"]
    assert guaranteed_qos(j)


def test_no_retries_and_deadlines(profiles, settings):
    j = job(profiles, settings)
    assert j["spec"]["backoffLimit"] == 0
    pod = j["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["activeDeadlineSeconds"] == 900 + settings.verify_timeout_s + settings.deadline_grace_s
    assert j["spec"]["activeDeadlineSeconds"] > pod["activeDeadlineSeconds"]


def test_locked_down(profiles, settings):
    pod = job(profiles, settings)["spec"]["template"]["spec"]
    c = pod["containers"][0]["securityContext"]
    assert c["readOnlyRootFilesystem"] and not c["allowPrivilegeEscalation"] and c["capabilities"]["drop"] == ["ALL"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False


def test_scratch_volumes_are_disk_backed_not_memory(profiles, settings):
    for v in job(profiles, settings)["spec"]["template"]["spec"]["volumes"]:
        assert "medium" not in v["emptyDir"], "a Memory emptyDir would count against the memory limit"


def test_api_key_only_by_secret_reference(profiles, settings):
    j = job(profiles, settings)
    env = j["spec"]["template"]["spec"]["containers"][0]["env"]
    key = next(e for e in env if e["name"] == "LLM_API_KEY")
    assert "value" not in key and key["valueFrom"]["secretKeyRef"]["name"] == settings.secret_name
    # the only place the key can appear is that reference; no env entry carries a literal secret
    literal = [e["name"] for e in env if "value" in e and "KEY" in e["name"].upper()]
    assert literal == []


def test_verify_mode_needs_no_key(profiles, settings):
    env = job(profiles, settings, mode="verify")["spec"]["template"]["spec"]["containers"][0]["env"]
    assert all(e["name"] != "LLM_API_KEY" for e in env)


def test_arch_selector_only_when_required(profiles, settings):
    assert "nodeSelector" not in job(profiles, settings, "tiny")["spec"]["template"]["spec"]
    assert job(profiles, settings, "ci_small")["spec"]["template"]["spec"]["nodeSelector"] == {"kubernetes.io/arch": "amd64"}


def test_labels_and_namespace(profiles, settings):
    j = job(profiles, settings)
    assert j["metadata"]["labels"]["app"] == "bench-constrained"
    assert j["metadata"]["namespace"] == settings.namespace == "bench-constrained"
    assert j["spec"]["template"]["metadata"]["labels"] == j["metadata"]["labels"]


def test_names_are_dns_safe_and_unique():
    a = job_name("0105-101010", "WEB-FEAT-01", "starve_256", 3)
    b = job_name("0105-101010", "WEB-FEAT-01", "starve_256", 2)
    assert a != b and len(a) <= 63 and a == a.lower() and "_" not in a
    assert len(job_name("x" * 100, "SYNTH:alloc_child:1024", "starve_256", 12)) <= 63
    assert all(len(v) <= 63 for v in labels("x" * 100, "T" * 100, "p", 1).values())
