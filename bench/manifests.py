"""Pure builders for the Kubernetes objects of one run cell (plain dicts, no client needed).

One Job per cell (task, profile, repeat). Requests equal limits on every resource, so the pod is
Guaranteed QoS and the limits are real: going over memory is an OOM kill, over CPU is throttling.
The API key is only ever referenced through secretKeyRef.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .config import Profile, Settings

APP_LABEL = "bench-constrained"
UID = 10001


def job_name(run_id: str, task_id: str, profile: str, repeat: int, attempt: int = 1) -> str:
    """DNS-safe, unique, at most 63 characters."""
    suffix = f"-a{attempt}" if attempt > 1 else ""
    base = re.sub(r"[^a-z0-9-]+", "-", f"b-{run_id}-{task_id}-{profile}-r{repeat}{suffix}".lower()).strip("-")
    digest = hashlib.sha1(f"{run_id}/{task_id}/{profile}/{repeat}/{attempt}".encode()).hexdigest()[:6]
    return f"{base[:56].rstrip('-')}-{digest}"


def labels(run_id: str, task_id: str, profile: str, repeat: int) -> dict[str, str]:
    safe = lambda s: re.sub(r"[^A-Za-z0-9_.-]+", "-", s)[:63].strip("-_.")  # noqa: E731
    return {
        "app": APP_LABEL,
        "bench/run": safe(run_id),
        "bench/task": safe(task_id),
        "bench/profile": safe(profile),
        "bench/repeat": str(repeat),
    }


def resources(profile: Profile, settings: Settings) -> dict[str, Any]:
    q = {"cpu": profile.cpu_limit, "memory": profile.memory, "ephemeral-storage": settings.ephemeral_storage}
    return {"requests": dict(q), "limits": dict(q)}


def build_job(
    *,
    run_id: str,
    task_id: str,
    profile: Profile,
    repeat: int,
    settings: Settings,
    timeout_s: int,
    mode: str = "agent",
    synth: str = "",
    deadline_s: int | None = None,
    image: str | None = None,
    code_configmap: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    name = job_name(run_id, synth.replace(":", "-") if synth else task_id, profile.name, repeat, attempt)
    lbls = labels(run_id, synth.replace(":", "-") if synth else task_id, profile.name, repeat)
    deadline = deadline_s or (timeout_s + settings.verify_timeout_s + settings.deadline_grace_s)

    env: list[dict[str, Any]] = [
        {"name": "BENCH_RUN_ID", "value": run_id},
        {"name": "BENCH_PROFILE", "value": profile.name},
        {"name": "BENCH_REPEAT", "value": str(repeat)},
        {"name": "BENCH_MODE", "value": mode},
        {"name": "BENCH_VERIFY_TIMEOUT_S", "value": str(settings.verify_timeout_s)},
        {"name": "HOME", "value": "/work/home"},
        {"name": "CARGO_HOME", "value": "/work/cargo"},
        {"name": "npm_config_cache", "value": "/tmp/npm-cache"},
    ]
    if synth:
        env.append({"name": "BENCH_SYNTH", "value": synth})
    else:
        env.append({"name": "BENCH_TASK", "value": task_id})
    if mode == "agent" and not synth:
        env.append({"name": "BENCH_AGENT_JSON", "value": json.dumps(settings.agent, separators=(",", ":"))})
        if settings.agent.get("harness", "loop") == "codex":
            # Codex never sees the model key: it talks to the translator, which has its own access key.
            ref = {"name": settings.agent["translator_secret_name"], "key": settings.agent.get("translator_secret_key", "key")}
            env.append({"name": "BENCH_TRANSLATOR_KEY", "valueFrom": {"secretKeyRef": ref}})
        else:
            env.append({"name": "LLM_API_KEY", "valueFrom": {"secretKeyRef": {
                "name": settings.secret_name, "key": settings.secret_key}}})

    node_selector = dict(settings.node_selector)
    if profile.arch != "any":
        node_selector["kubernetes.io/arch"] = profile.arch

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        # Pod-level deadline: the pod ends Failed/DeadlineExceeded but stays, so its logs survive.
        "activeDeadlineSeconds": deadline,
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": UID, "runAsGroup": UID, "fsGroup": UID,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "task",
            "image": image or settings.image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["python3", "-m", "bench.podrun"],
            "env": env,
            "resources": resources(profile, settings),
            "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "volumeMounts": [{"name": "work", "mountPath": "/work"}, {"name": "tmp", "mountPath": "/tmp"}],
        }],
        # Disk-backed emptyDirs on purpose: a Memory-backed one would count against the memory limit.
        "volumes": [
            {"name": "work", "emptyDir": {"sizeLimit": settings.work_size_limit}},
            {"name": "tmp", "emptyDir": {"sizeLimit": settings.tmp_size_limit}},
        ],
    }
    if code_configmap:   # run the runner from a ConfigMap on a stock image: no custom image needed
        c = pod_spec["containers"][0]
        c["env"].append({"name": "PYTHONPATH", "value": "/opt/bench"})
        c["env"].append({"name": "BENCH_ROOT", "value": "/opt/bench"})
        c["env"].append({"name": "PYTHONDONTWRITEBYTECODE", "value": "1"})
        c["volumeMounts"].append({"name": "code", "mountPath": "/opt/bench/bench", "readOnly": True})
        pod_spec["volumes"].append({"name": "code", "configMap": {"name": code_configmap}})
    if node_selector:
        pod_spec["nodeSelector"] = node_selector
    if settings.allowed_nodes:      # hard requirement: these nodes and no others
        pod_spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In",
                                                         "values": list(settings.allowed_nodes)}]}]}}}
    if settings.priority_class:     # a low class means anyone else's pod can preempt ours, never the reverse
        pod_spec["priorityClassName"] = settings.priority_class
    if settings.tolerations:
        pod_spec["tolerations"] = list(settings.tolerations)
    if settings.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [{"name": n} for n in settings.image_pull_secrets]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": settings.namespace, "labels": lbls},
        "spec": {
            "backoffLimit": 0,                      # no silent retries: a retry would hide an OOM
            "activeDeadlineSeconds": deadline + 60,   # backstop; the pod deadline fires first
            "ttlSecondsAfterFinished": 86400,       # safety net; `bench clean` normally removes it
            "template": {"metadata": {"labels": dict(lbls)}, "spec": pod_spec},
        },
    }


def guaranteed_qos(job: dict[str, Any]) -> bool:
    """True when every container has requests equal to limits for cpu and memory."""
    for c in job["spec"]["template"]["spec"]["containers"]:
        r = c["resources"]
        for key in ("cpu", "memory"):
            if r["requests"].get(key) != r["limits"].get(key) or key not in r["limits"]:
                return False
    return True


TRANSLATOR_NAME = "bench-translator"
TRANSLATOR_PORT = 4000


def build_translator(settings: Settings) -> list[dict[str, Any]]:
    """ConfigMap + Deployment + Service for the Responses-to-Chat translator that Codex talks to.

    Labelled app=bench-translator, NOT app=bench-constrained, so `bench clean` never removes it. Sized and pinned like the
    benchmark pods: requests equal limits, only the allowed nodes, low priority. Its keys come from Secrets only.
    """
    a = settings.agent
    cfg = (f"model_list:\n  - model_name: {a['model']}\n    litellm_params:\n      model: openai/{a['model']}\n"
           f"      api_base: {a['base_url']}\n      api_key: os.environ/SOKET_API_KEY\n      use_chat_completions_api: true\n"
           "litellm_settings:\n  drop_params: true\n")
    labels = {"app": TRANSLATOR_NAME}
    res = {"cpu": "1", "memory": "1536Mi", "ephemeral-storage": "1Gi"}
    pod: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": UID, "runAsGroup": UID, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "translator", "image": settings.translator_image, "imagePullPolicy": "IfNotPresent",
            "ports": [{"containerPort": TRANSLATOR_PORT}],
            "env": [
                {"name": "SOKET_API_KEY", "valueFrom": {"secretKeyRef": {"name": settings.secret_name, "key": settings.secret_key}}},
                {"name": "LITELLM_MASTER_KEY", "valueFrom": {"secretKeyRef": {
                    "name": a["translator_secret_name"], "key": a.get("translator_secret_key", "key")}}},
                {"name": "HOME", "value": "/tmp"},
            ],
            "resources": {"requests": dict(res), "limits": dict(res)},
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "readinessProbe": {"httpGet": {"path": "/health/readiness", "port": TRANSLATOR_PORT}, "periodSeconds": 10},
            "volumeMounts": [{"name": "config", "mountPath": "/config", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}],
        }],
        "volumes": [{"name": "config", "configMap": {"name": f"{TRANSLATOR_NAME}-config"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}}],
    }
    if settings.allowed_nodes:
        pod["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": list(settings.allowed_nodes)}]}]}}}
    if settings.node_selector:
        pod["nodeSelector"] = dict(settings.node_selector)
    if settings.priority_class:
        pod["priorityClassName"] = settings.priority_class
    if settings.image_pull_secrets:
        pod["imagePullSecrets"] = [{"name": n} for n in settings.image_pull_secrets]
    ns = settings.namespace
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{TRANSLATOR_NAME}-config", "namespace": ns, "labels": labels},
         "data": {"litellm.yaml": cfg}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": TRANSLATOR_NAME, "namespace": ns, "labels": labels},
         "spec": {"replicas": 1, "selector": {"matchLabels": labels}, "template": {"metadata": {"labels": dict(labels)}, "spec": pod}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": TRANSLATOR_NAME, "namespace": ns, "labels": labels},
         "spec": {"selector": labels, "ports": [{"port": TRANSLATOR_PORT, "targetPort": TRANSLATOR_PORT}]}},
    ]
