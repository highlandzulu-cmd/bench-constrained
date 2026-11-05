"""The only module that talks to the Kubernetes API. Everything it touches lives in one namespace
and carries the label app=bench-constrained."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .manifests import APP_LABEL
from .results import INFRA_WAITING_REASONS, PodOutcome

PERSISTENT_WAITING = {"ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "ErrImageNeverPull"}


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    return (b - a).total_seconds() if a and b else None


class Cluster:
    def __init__(self, namespace: str, kubeconfig: str | None = None, context: str | None = None,
                 poll_s: float = 3.0) -> None:
        from kubernetes import client, config  # imported late so tests and `bench list` need no cluster

        try:
            config.load_kube_config(config_file=kubeconfig, context=context)
        except Exception:  # noqa: BLE001
            config.load_incluster_config()
        self.ns = namespace
        self.batch = client.BatchV1Api()
        self.core = client.CoreV1Api()
        self.poll_s = poll_s

    # -- objects -----------------------------------------------------------------------------
    def create_job(self, job: dict[str, Any]) -> None:
        if job["metadata"]["namespace"] != self.ns:
            raise ValueError(f"refusing to create in namespace {job['metadata']['namespace']!r}; only {self.ns!r}")
        self.batch.create_namespaced_job(self.ns, job)

    def create_configmap(self, name: str, data: dict[str, str], run_id: str) -> None:
        from kubernetes import client

        cm = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name, namespace=self.ns,
                                                              labels={"app": APP_LABEL, "bench/run": run_id}), data=data)
        self.core.create_namespaced_config_map(self.ns, cm)

    def delete_configmap(self, name: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.core.delete_namespaced_config_map(name, self.ns)
        except ApiException as e:
            if e.status != 404:
                raise

    def delete_job(self, name: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.batch.delete_namespaced_job(name, self.ns, propagation_policy="Background")
        except ApiException as e:
            if e.status != 404:
                raise

    def clean(self, run_id: str | None = None) -> int:
        """Delete only Jobs this tool created (label app=bench-constrained), optionally one run."""
        selector = f"app={APP_LABEL}" + (f",bench/run={run_id}" if run_id else "")
        jobs = self.batch.list_namespaced_job(self.ns, label_selector=selector).items
        for j in jobs:
            self.delete_job(j.metadata.name)
        cms = self.core.list_namespaced_config_map(self.ns, label_selector=selector).items
        for c in cms:
            self.delete_configmap(c.metadata.name)
        return len(jobs)

    # -- read-only inspection ----------------------------------------------------------------
    def inspect_nodes(self, names: list[str]):
        """Per node: what is running and requested there. Read-only."""
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        from .preflight import NodeUse, node_use_from_api

        uses = []
        for name in names:
            try:
                node = self.core.read_node(name)
            except ApiException as e:
                if e.status == 404:
                    uses.append(NodeUse(name=name, found=False))
                    continue
                raise
            pods = self.core.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={name},status.phase!=Succeeded,status.phase!=Failed").items
            uses.append(node_use_from_api(node, pods))
        return uses

    def priority_class_value(self, name: str):
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        try:
            return client.SchedulingV1Api().read_priority_class(name).value
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    # -- waiting and reading -----------------------------------------------------------------
    def wait(self, name: str, pending_timeout_s: int, max_wait_s: int) -> PodOutcome:
        """Poll until the pod finishes (or can never start), then describe what happened."""
        from kubernetes.client.rest import ApiException

        t0 = time.time()
        pod = None
        while True:
            pods = self.core.list_namespaced_pod(self.ns, label_selector=f"job-name={name}").items
            pod = pods[0] if pods else None
            job = self.batch.read_namespaced_job_status(name, self.ns)
            failed_reason = next((c.reason for c in (job.status.conditions or []) if c.type == "Failed"), None)
            if pod is not None:
                phase = pod.status.phase
                cs = (pod.status.container_statuses or [None])[0]
                waiting = cs.state.waiting.reason if cs and cs.state and cs.state.waiting else None
                if phase in ("Succeeded", "Failed"):
                    break
                if waiting in PERSISTENT_WAITING:
                    break
                if phase == "Pending" and time.time() - t0 > pending_timeout_s:
                    return self._outcome(name, pod, failed_reason, pending_timed_out=True)
            elif failed_reason or job.status.succeeded:
                break      # pod already gone (Job deadline); nothing more to read
            if time.time() - t0 > max_wait_s:
                return self._outcome(name, pod, failed_reason or "WaitTimeout", pending_timed_out=False)
            time.sleep(self.poll_s)
        return self._outcome(name, pod, failed_reason)

    def _outcome(self, name: str, pod: Any, job_failed_reason: str | None, pending_timed_out: bool = False) -> PodOutcome:
        from kubernetes.client.rest import ApiException

        out = PodOutcome(job_failed_reason=job_failed_reason, pending_timed_out=pending_timed_out)
        if pod is None:
            return out
        out.node = pod.spec.node_name
        out.pod_reason = pod.status.reason
        out.pod_message = pod.status.message
        cs = (pod.status.container_statuses or [None])[0]
        if cs is not None:
            st = cs.state
            if st and st.terminated:
                t = st.terminated
                out.exit_code, out.terminated_reason, out.termination_message = t.exit_code, t.reason, t.message
                out.queue_wait_s = _secs(pod.metadata.creation_timestamp, t.started_at)
                out.k8s_wall_s = _secs(t.started_at, t.finished_at)
            elif st and st.waiting:
                out.waiting_reason = st.waiting.reason
        try:
            out.logs = self.core.read_namespaced_pod_log(pod.metadata.name, self.ns, container="task") or ""
        except ApiException:
            out.logs = ""
        return out
