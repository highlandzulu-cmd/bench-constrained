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
    dirs: list[str] = field(default_factory=list)             # folders to create first (some harnesses need them to exist)


# Which environment variable must hold a key for each harness (checked before it is started).
REQUIRED_ENV = {"loop": "LLM_API_KEY", "dsh": "LLM_API_KEY", "codex": "BENCH_TRANSLATOR_KEY", "pi": "LLM_API_KEY", "goose": "LLM_API_KEY", "cline": "LLM_API_KEY"}


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
                       "BENCH_API_KEY": env.get("LLM_API_KEY", "")}, {str(patch): text}, [str(home / "dsh-home")])


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
    return LaunchSpec(cmd, {"CODEX_HOME": str(home / "codex-home")}, {}, [str(home / "codex-home")])


def pi(prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    """Pi: a models.json in its agent folder declares the OpenAI-compatible endpoint; it expands $BENCH_API_KEY itself."""
    agent = home / "pi-agent"
    models = {"providers": {"bench": {"baseUrl": a["base_url"], "api": "openai-completions", "apiKey": "$BENCH_API_KEY",
                                      "models": [{"id": a["model"]}]}}}
    cmd = [a.get("pi_bin", "pi"), "--print", prompt, "--provider", "bench", "--model", a["model"], "--no-session"]
    return LaunchSpec(cmd, {"PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1", "BENCH_API_KEY": env.get("LLM_API_KEY", "")},
                      {str(agent / "models.json"): json.dumps(models)}, [str(agent)])


def goose(prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    """Goose: its `openai` provider with the host and path pointed at the model endpoint; everything through environment
    variables, keyring off, autonomous mode, no session saved."""
    from urllib.parse import urlsplit
    u = urlsplit(a["base_url"])
    host = f"{u.scheme}://{u.netloc}"
    path = (u.path.strip("/") + "/chat/completions").lstrip("/")
    cmd = [a.get("goose_bin", "goose"), "run", "-t", prompt, "-q", "--provider", "openai", "--model", a["model"],
           "--no-session", "--with-builtin", "developer"]
    return LaunchSpec(cmd, {"GOOSE_PATH_ROOT": str(home / "goose-root"), "GOOSE_DISABLE_KEYRING": "1", "GOOSE_MODE": "auto",
                            "GOOSE_PROVIDER": "openai", "OPENAI_HOST": host, "OPENAI_BASE_PATH": path,
                            "OPENAI_API_KEY": env.get("LLM_API_KEY", "")}, {}, [str(home / "goose-root")])


def cline(prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    """Cline: its provider choice is one JSON file in its data folder. The pod writes it at start from the key in the
    environment (never baked into the image, never on a command line)."""
    data, conf = home / "cline" / "data", home / "cline" / "config"
    providers = {"version": 1, "lastUsedProvider": "openai-compatible", "modes": {}, "providers": {"openai-compatible": {
        "settings": {"provider": "openai-compatible", "apiKey": env.get("LLM_API_KEY", ""), "model": a["model"], "baseUrl": a["base_url"]},
        "updatedAt": "2026-01-01T00:00:00.000Z", "tokenSource": "manual"}}}
    cmd = [a.get("cline_bin", "cline"), "--json", "--data-dir", str(data), "--config", str(conf), "-P", "openai", "-m", a["model"],
           "--cwd", str(work), prompt]
    return LaunchSpec(cmd, {}, {str(data / "settings" / "providers.json"): json.dumps(providers)}, [str(conf)])


BUILDERS: dict[str, Callable[..., LaunchSpec]] = {"loop": loop, "dsh": dsh, "codex": codex, "pi": pi, "goose": goose, "cline": cline}


def launch(harness: str, prompt: str, work: Path, home: Path, a: dict[str, Any], env: dict[str, str]) -> LaunchSpec:
    required_env(harness)
    return BUILDERS[harness](prompt, work, home, a, env)
