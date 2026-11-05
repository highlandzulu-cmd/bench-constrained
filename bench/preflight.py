"""Read-only check of where benchmark pods may run and who else is already there.

Nothing here creates, changes or deletes anything. It answers: which nodes can our pods land on, what
is already running on them (which namespaces), how much CPU, memory and GPU is already requested, and
how large the benchmark's worst case would be next to that.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .config import Profile, Settings, _quantity_to_mib

BENCH_SHARE_WARN = 0.10     # warn when the benchmark could take more than 10% of what is still free


def parse_cpu(q: Any) -> float:
    s = str(q).strip()
    return float(s[:-1]) / 1000 if s.endswith("m") else float(s or 0)


def parse_mem_mib(q: Any) -> float:
    return _quantity_to_mib(str(q)) if str(q).strip() else 0.0


@dataclass
class NodeUse:
    name: str
    found: bool = True
    taints: list[str] = field(default_factory=list)
    alloc_cpu: float = 0.0
    alloc_mem_mib: float = 0.0
    alloc_gpu: int = 0
    alloc_disk_mib: float = 0.0
    req_cpu: float = 0.0
    req_mem_mib: float = 0.0
    req_gpu: int = 0
    req_disk_mib: float = 0.0
    pods: int = 0
    by_namespace: Counter = field(default_factory=Counter)


def node_use_from_api(node: Any, pods: list[Any]) -> NodeUse:
    alloc = node.status.allocatable or {}
    use = NodeUse(name=node.metadata.name,
                  taints=[f"{t.key}={t.value}:{t.effect}" for t in (node.spec.taints or [])],
                  alloc_cpu=parse_cpu(alloc.get("cpu", 0)), alloc_mem_mib=parse_mem_mib(alloc.get("memory", 0)),
                  alloc_gpu=int(alloc.get("nvidia.com/gpu", 0) or 0),
                  alloc_disk_mib=parse_mem_mib(alloc.get("ephemeral-storage", 0)))
    for p in pods:
        use.pods += 1
        use.by_namespace[p.metadata.namespace] += 1
        for c in p.spec.containers:
            req = (c.resources.requests if c.resources and c.resources.requests else {}) or {}
            use.req_cpu += parse_cpu(req.get("cpu", 0))
            use.req_mem_mib += parse_mem_mib(req.get("memory", 0))
            use.req_gpu += int(req.get("nvidia.com/gpu", 0) or 0)
            use.req_disk_mib += parse_mem_mib(req.get("ephemeral-storage", 0))
    return use


def worst_case(settings: Settings, profiles: list[Profile]) -> tuple[float, float]:
    """CPU and MiB if max_parallel pods of the most demanding chosen profile all landed on one node."""
    cpu = max(parse_cpu(p.cpu_limit) for p in profiles)
    mem = max(p.memory_mib for p in profiles)
    return cpu * settings.max_parallel, mem * settings.max_parallel


def worst_case_disk_mib(settings: Settings) -> float:
    """Scratch space if max_parallel pods all landed on one node (each may use up to its ephemeral-storage limit)."""
    return parse_mem_mib(settings.ephemeral_storage) * settings.max_parallel


def assess(use: NodeUse, bench_cpu: float, bench_mem_mib: float, bench_disk_mib: float = 0.0) -> list[tuple[str, str]]:
    """[(level, message)] with level ok, warn or stop."""
    if not use.found:
        return [("stop", f"node {use.name} does not exist")]
    out: list[tuple[str, str]] = []
    free_cpu, free_mem = use.alloc_cpu - use.req_cpu, use.alloc_mem_mib - use.req_mem_mib
    others = {ns: n for ns, n in use.by_namespace.items() if ns != "bench-constrained"}
    out.append(("ok", f"{use.pods} running pods ({', '.join(f'{ns}:{n}' for ns, n in sorted(others.items())) or 'none from others'})"))
    out.append(("ok", f"CPU requested {use.req_cpu:.1f} of {use.alloc_cpu:.0f}; memory {use.req_mem_mib / 1024:.0f} of "
                      f"{use.alloc_mem_mib / 1024:.0f} GiB; GPUs {use.req_gpu} of {use.alloc_gpu} (we request none)"))
    if bench_cpu > free_cpu or bench_mem_mib > free_mem:
        out.append(("stop", f"benchmark worst case ({bench_cpu:g} CPU, {bench_mem_mib / 1024:g} GiB) does not fit in what is free"))
    elif bench_cpu > BENCH_SHARE_WARN * free_cpu or bench_mem_mib > BENCH_SHARE_WARN * free_mem:
        out.append(("warn", f"benchmark worst case ({bench_cpu:g} CPU, {bench_mem_mib / 1024:g} GiB) is over "
                            f"{int(BENCH_SHARE_WARN * 100)}% of what is still free"))
    else:
        out.append(("ok", f"benchmark worst case ({bench_cpu:g} CPU, {bench_mem_mib / 1024:g} GiB) is a small share of what is free"))
    if use.alloc_disk_mib:
        free_disk = use.alloc_disk_mib - use.req_disk_mib
        msg = (f"scratch disk: others have requested {use.req_disk_mib / 1024:.0f} of {use.alloc_disk_mib / 1024:.0f} GiB; "
               f"our worst case adds {bench_disk_mib / 1024:g} GiB (plus the image download)")
        if bench_disk_mib > free_disk:
            out.append(("stop", msg + ", which does not fit"))
        elif bench_disk_mib > BENCH_SHARE_WARN * free_disk:
            out.append(("warn", msg + ", over 10% of what is free"))
        else:
            out.append(("ok", msg))
    if use.taints:
        out.append(("warn", "taints: " + ", ".join(use.taints) + " (pods need matching tolerations in settings.yaml)"))
    return out


def scope_problem(settings: Settings) -> str | None:
    """None when runs are confined to named nodes; otherwise why they would not be."""
    if settings.allowed_nodes or settings.node_selector:
        return None
    return ("neither allowed_nodes nor node_selector is set in config/settings.yaml, so pods could land on any "
            "node in the cluster, including other teams' nodes. Set allowed_nodes and try again.")
