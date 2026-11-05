"""How to start each harness inside the pod. Each builder returns a LaunchSpec: the command, extra environment
variables, and config files to write first. The model key arrives as an environment variable from a Secret; where a
harness needs it inside a config file, the file is written at start-up in the pod's scratch space, never baked in.

Pod-side code: standard library only (the stock-image self-test runs without PyYAML).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class LaunchSpec:
    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)       # absolute path -> content, written before the start


# Which environment variable must hold a key for each harness (checked before it is started).
REQUIRED_ENV = {"loop": "LLM_API_KEY", "dsh": "LLM_API_KEY", "codex": "BENCH_TRANSLATOR_KEY"}


def required_env(harness: str) -> str:
    try:
        return REQUIRED_ENV[harness]
    except KeyError:
        raise ValueError(f"unknown harness {harness!r}; known: {sorted(REQUIRED_ENV)}") from None


def loop(prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    base_url, model = a["base_url"], a["model"]
    think = str(a.get("thinking_level", "off"))
    agent_dir = home / "loop-agent"
    settings = {"responseHeaderTimeoutMs": int(a.get("response_header_timeout_ms", 600000)),
                "defaultThinkingLevel": think, "providers": [{"id": "bench", "name": "bench", "baseUrl": base_url}]}
    model_entry: dict[str, Any] = {"id": model}
    if think != "off":
        model_entry["reasoning"] = True
    models = {"providers": [{"id": "bench", "baseUrl": base_url, "apiKeyEnv": ["BENCH_API_KEY"], "models": [model_entry]}]}
    cmd = [a.get("loop_bin", "/opt/harness/bin/loop"), "--cwd", str(work), "--provider", "bench", "--model", model,
           "--permission-mode", "full-access", "--no-context-files", "--no-web", *a.get("extra_args", []), "--print", prompt]
    return LaunchSpec(cmd, {"LOOP_CODING_AGENT_DIR": str(agent_dir), "LOOP_CODING_AGENT_SESSION_DIR": str(home / "loop-sessions"),
                            "BENCH_API_KEY": env.get("LLM_API_KEY", "")},
                      {str(agent_dir / "settings.json"): json.dumps(settings), str(agent_dir / "models.json"): json.dumps(models)})


def dsh(prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    """DeepSeek Harness: a patch overlay declares the custom provider; full access comes from DSH_PERMISSION_MODE."""
    patch = home / "bench.patch.yml"
    text = f"""- id: agent-default-model
  config:
    provider: custom
    model: "{a['model']}"
- id: llm-pi-ai
  config:
    providers:
      custom:
        apiKeyEnv: BENCH_API_KEY
        api: openai-completions
        baseURL: "{a['base_url']}"
        models:
          - id: "{a['model']}"
- id: bash-sandbox
  config:
    timeoutMs: 600000
- id: otel
  disabled: true
- id: session-telemetry-otel
  disabled: true
- id: tool-web
  disabled: true
"""
    return LaunchSpec([a.get("dsh_bin", "dsh"), "--profile", "headless", "--patch", str(patch), prompt],
                      {"DSH_HOME": str(home / "dsh-home"), "DSH_PERMISSION_MODE": "danger-full-access",
                       "BENCH_API_KEY": env.get("LLM_API_KEY", "")}, {str(patch): text})


def codex(prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    """Codex speaks only the Responses API, which the model endpoint lacks, so it talks to a translator service
    (LiteLLM, see deploy/translator.yaml) that turns Responses calls into chat calls. The pod itself is the sandbox,
    so Codex's own sandbox is switched off."""
    cmd = [a.get("codex_bin", "codex"), "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
           "--ignore-user-config", "--ignore-rules", "-C", str(work)]
    for kv in ('model_provider="bench"', 'model_providers.bench.name="Soket via translator"',
               f'model_providers.bench.base_url="{a["translator_url"]}"', 'model_providers.bench.env_key="BENCH_TRANSLATOR_KEY"',
               'model_providers.bench.wire_api="responses"'):
        cmd += ["-c", kv]
    cmd += ["-m", a["model"], prompt]
    return LaunchSpec(cmd, {"CODEX_HOME": str(home / "codex-home")}, {})


BUILDERS: dict[str, Callable[..., LaunchSpec]] = {"loop": loop, "dsh": dsh, "codex": codex}


def launch(harness: str, prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    required_env(harness)
    return BUILDERS[harness](prompt, work, home, a, env)
