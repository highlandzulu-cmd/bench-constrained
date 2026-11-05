import json
from pathlib import Path

import pytest

from bench import harnesses
from bench.manifests import build_job

A = {"base_url": "https://models.example/v1", "model": "GLM-5.3", "thinking_level": "off",
     "translator_url": "http://translator:4000/v1", "translator_secret_name": "tk", "translator_secret_key": "key"}
WORK, HOME = Path("/work/task"), Path("/work/home")
SECRET = "sk-THE-REAL-KEY"
ENV = {"LLM_API_KEY": SECRET, "BENCH_TRANSLATOR_KEY": "sk-translator"}


def spec(name):
    return harnesses.launch(name, "FIX THE BUG", WORK, HOME, A, ENV)


@pytest.mark.parametrize("name", ["loop", "dsh", "codex"])
def test_the_prompt_is_passed_and_the_work_folder_is_used(name):
    s = spec(name)
    assert "FIX THE BUG" in s.cmd and (str(WORK) in s.cmd or name == "dsh")


@pytest.mark.parametrize("name", ["loop", "dsh", "codex"])
def test_no_key_ever_appears_on_a_command_line(name):
    s = spec(name)
    assert not any(SECRET in part or "sk-translator" in part for part in s.cmd)


def test_dsh_uses_the_latest_interface_and_declares_the_custom_provider():
    s = spec("dsh")
    assert s.cmd[:3] == ["dsh", "--profile", "headless"] and "--patch" in s.cmd
    patch = next(iter(s.files.values()))
    assert "baseURL: \"https://models.example/v1\"" in patch and "apiKeyEnv: BENCH_API_KEY" in patch
    assert SECRET not in patch                                        # the key stays in the environment
    assert s.env["DSH_PERMISSION_MODE"] == "danger-full-access" and s.env["BENCH_API_KEY"] == SECRET


def test_codex_goes_through_the_translator_with_the_responses_api():
    s = spec("codex")
    joined = " ".join(s.cmd)
    assert 'wire_api="responses"' in joined and 'base_url="http://translator:4000/v1"' in joined
    assert 'env_key="BENCH_TRANSLATOR_KEY"' in joined and "api.soket" not in joined and "models.example" not in joined
    assert "--dangerously-bypass-approvals-and-sandbox" in s.cmd       # the pod is the sandbox
    assert SECRET not in json.dumps([s.cmd, s.env, s.files])           # Codex never sees the model key


def test_loop_is_unchanged():
    s = spec("loop")
    assert s.cmd[0] == "/opt/harness/bin/loop" and "--permission-mode" in s.cmd and s.env["BENCH_API_KEY"] == SECRET
    assert any(p.endswith("models.json") for p in s.files)


def test_unknown_harness_is_rejected_clearly():
    with pytest.raises(ValueError, match="unknown harness"):
        harnesses.launch("nope", "x", WORK, HOME, A, ENV)


def job_env(profiles, settings, harness):
    agent = {**settings.agent, "harness": harness}
    import dataclasses
    j = build_job(run_id="r", task_id="BUG-01", profile=profiles["tiny"], repeat=1,
                  settings=dataclasses.replace(settings, agent=agent), timeout_s=900)
    return {e["name"]: e for e in j["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_codex_pods_get_only_the_translator_key(profiles, settings):
    env = job_env(profiles, settings, "codex")
    assert "LLM_API_KEY" not in env
    ref = env["BENCH_TRANSLATOR_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "bench-translator-key", "key": "key"} and "value" not in env["BENCH_TRANSLATOR_KEY"]


@pytest.mark.parametrize("harness", ["loop", "dsh"])
def test_other_harnesses_get_the_model_key_only_by_reference(profiles, settings, harness):
    env = job_env(profiles, settings, harness)
    assert "BENCH_TRANSLATOR_KEY" not in env and "value" not in env["LLM_API_KEY"]
    assert json.loads(env["BENCH_AGENT_JSON"]["value"])["harness"] == harness


def test_the_missing_key_check_is_per_harness(tmp_path):
    from bench.podrun import PodConfig, run
    cfg = PodConfig(task_id="BUG-01", mode="agent", fixtures_dir=Path(__file__).resolve().parent.parent / "fixtures",
                    work_dir=tmp_path, progress_every_s=999, cgroup_root=str(tmp_path / "nocg"), agent={**A, "harness": "codex"})
    r = run(cfg, env={"LLM_API_KEY": "has-a-model-key-but-codex-needs-the-translator-key"})
    assert r["in_pod_status"] == "error" and "BENCH_TRANSLATOR_KEY" in r["error"]


def test_translator_manifest(settings):
    from bench.manifests import build_translator
    cm, dep, svc = build_translator(settings)
    assert {o["kind"] for o in (cm, dep, svc)} == {"ConfigMap", "Deployment", "Service"}
    assert all(o["metadata"]["namespace"] == "bench-constrained" for o in (cm, dep, svc))
    assert all(o["metadata"]["labels"] == {"app": "bench-translator"} for o in (cm, dep, svc))   # never matched by `bench clean`
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["requests"] == c["resources"]["limits"]
    env = {e["name"]: e for e in c["env"]}
    assert all("value" not in env[k] for k in ("SOKET_API_KEY", "LITELLM_MASTER_KEY"))             # keys only by reference
    assert env["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "bench-translator-key"
    pod = dep["spec"]["template"]["spec"]
    assert pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0][
        "matchExpressions"][0]["values"] == settings.allowed_nodes
    assert "use_chat_completions_api: true" in cm["data"]["litellm.yaml"] and "openai/GLM-5.3" in cm["data"]["litellm.yaml"]
    # the URL Codex is given must be this Service
    assert f"http://{svc['metadata']['name']}.bench-constrained.svc.cluster.local:{svc['spec']['ports'][0]['port']}/v1" == settings.agent["translator_url"]


def test_translator_clean_label_is_separate(settings):
    from bench.manifests import APP_LABEL, build_translator
    assert all(o["metadata"]["labels"]["app"] != APP_LABEL for o in build_translator(settings))


def test_translator_config_is_valid_yaml_with_the_tested_settings(settings):
    import yaml
    from bench.manifests import build_translator
    cfg = yaml.safe_load(build_translator(settings)[0]["data"]["litellm.yaml"])
    p = cfg["model_list"][0]["litellm_params"]
    assert cfg["model_list"][0]["model_name"] == "GLM-5.3" and p["model"] == "openai/GLM-5.3"
    assert p["use_chat_completions_api"] is True and p["api_key"] == "os.environ/SOKET_API_KEY" and cfg["litellm_settings"]["drop_params"] is True


def test_pods_always_check_the_registry_so_a_rebuilt_tag_is_never_stale(profiles, settings):
    """The image tag is re-published on each build; IfNotPresent would keep a node's old copy and miss new harnesses."""
    from bench.manifests import build_translator
    job = build_job(run_id="r", task_id="BUG-01", profile=profiles["tiny"], repeat=1, settings=settings, timeout_s=900)
    assert job["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] == "Always"
    assert build_translator(settings)[1]["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"] == "Always"


def test_harness_home_folders_are_created_before_starting(tmp_path):
    """DeepSeek Harness and Codex need their home folder to exist; the local tests made it by hand, the pod did not."""
    from bench.podrun import PodConfig, run_agent
    from bench.tasks import Task
    for name, folder in (("dsh", "dsh-home"), ("codex", "codex-home")):
        s = harnesses.launch(name, "x", WORK, tmp_path / "home", A, ENV)
        assert str(tmp_path / "home" / folder) in s.dirs


def test_a_harness_that_cannot_start_is_not_a_task_failure():
    from bench.podrun import detect_launch_error
    assert "not found" in detect_launch_error(127, "sh: 1: dsh: not found\n")
    assert "ENOENT" in detect_launch_error(1, "Error: ENOENT: no such file or directory, mkdir '/x'\n")
    assert detect_launch_error(0, "ENOENT somewhere in a successful run") is None
    assert detect_launch_error(1, "tests failed, agent gave up\n") is None


def test_launch_error_becomes_an_infra_error_with_the_reason():
    from bench.results import PodOutcome, classify
    import json as _json
    out = PodOutcome(exit_code=0, logs="BENCH_RESULT " + _json.dumps({"in_pod_status": "launch_error", "agent_launch_error": "sh: dsh: not found"}))
    rec = classify({"task_id": "BUG-01"}, out)
    assert rec["status"] == "infra_error" and "could not start" in " ".join(rec["notes"]) and "dsh: not found" in " ".join(rec["notes"])
    assert rec["retryable"] is False                                    # our setup problem; retrying would repeat it


def test_a_silent_instant_success_is_not_a_result():
    from bench.podrun import detect_silent_exit
    assert "without any output" in detect_silent_exit(0, 0.2, "")
    assert detect_silent_exit(0, 0.2, "loop: did the work") is None          # it said something
    assert detect_silent_exit(0, 40.0, "") is None                            # long enough to have worked quietly
    assert detect_silent_exit(1, 0.2, "") is None                             # a failure is reported by the other checks


def test_the_image_uses_node_24_and_checks_the_harness_really_runs():
    text = (Path(__file__).resolve().parent.parent / "image" / "Dockerfile").read_text()
    assert "node:24-bookworm-slim" in text and "node:20" not in text
    assert 'test "$(dsh --version)" = "0.2.2-rc.1"' in text            # a silent no-op dsh now fails the build


def test_the_pod_user_has_a_passwd_entry():
    """uid 10001 with no passwd entry makes os.userInfo() throw ENOENT, and DeepSeek Harness then exits silently."""
    text = (Path(__file__).resolve().parent.parent / "image" / "Dockerfile").read_text()
    assert "useradd -u 10001" in text and "getent passwd 10001" in text
    from bench.manifests import UID
    assert UID == 10001
