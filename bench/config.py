"""Loading the YAML config files into small typed objects."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def load_yaml(path: Path) -> Any:
    import yaml  # imported here so code that only needs the constants runs without PyYAML (stock pod image)

    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _quantity_to_mib(q: str) -> float:
    """Kubernetes memory quantity (Mi, Gi, M, G, bytes) to MiB."""
    m = re.fullmatch(r"([0-9.]+)\s*([A-Za-z]*)", str(q).strip())
    if not m:
        raise ValueError(f"bad memory quantity {q!r}")
    num, unit = float(m.group(1)), m.group(2)
    factors = {"": 1 / 1048576, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1048576,
               "k": 1000 / 1048576, "M": 1e6 / 1048576, "G": 1e9 / 1048576}
    if unit not in factors:
        raise ValueError(f"unknown memory unit {unit!r} in {q!r}")
    return num * factors[unit]


@dataclass(frozen=True)
class Profile:
    name: str
    cpu: str
    memory: str
    arch: str = "any"
    cpu_scale: float = 1.0

    @property
    def emulated(self) -> bool:
        return self.cpu_scale != 1.0

    @property
    def cpu_limit(self) -> str:
        """CPU limit as a Kubernetes quantity, after cpu_scale (millicores when fractional)."""
        value = float(self.cpu) * self.cpu_scale
        milli = round(value * 1000)
        return str(milli // 1000) if milli % 1000 == 0 else f"{milli}m"

    @property
    def memory_mib(self) -> float:
        return _quantity_to_mib(self.memory)


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    data = load_yaml(path or CONFIG_DIR / "profiles.yaml")
    out: dict[str, Profile] = {}
    for name, p in data["profiles"].items():
        out[name] = Profile(name=name, cpu=str(p["cpu"]), memory=str(p["memory"]),
                            arch=str(p.get("arch", "any")), cpu_scale=float(p.get("cpu_scale", 1.0)))
    return out


@dataclass
class Settings:
    namespace: str
    image: str
    image_pull_secrets: list[str] = field(default_factory=list)
    node_selector: dict[str, str] = field(default_factory=dict)
    allowed_nodes: list[str] = field(default_factory=list)
    priority_class: str = ""
    tolerations: list[dict[str, Any]] = field(default_factory=list)
    max_parallel: int = 4
    repeats: int = 3
    pending_timeout_s: int = 600
    deadline_grace_s: int = 300
    verify_timeout_s: int = 300
    infra_retries: int = 2
    infra_retry_wait_s: int = 30
    ephemeral_storage: str = "4Gi"
    work_size_limit: str = "2Gi"
    tmp_size_limit: str = "256Mi"
    secret_name: str = "bench-api-key"
    secret_key: str = "api-key"
    agent: dict[str, Any] = field(default_factory=dict)
    results_dir: str = "results"


def load_settings(path: Path | None = None) -> Settings:
    """config/settings.yaml, with config/settings.local.yaml (not committed: node names, priority class) on top."""
    base = path or CONFIG_DIR / "settings.yaml"
    data = load_yaml(base)
    local = base.with_name("settings.local.yaml")
    if local.exists():
        data.update(load_yaml(local) or {})
    known = {f for f in Settings.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown settings keys: {sorted(unknown)}")
    return Settings(**data)
