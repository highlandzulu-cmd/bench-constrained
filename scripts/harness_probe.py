#!/usr/bin/env python3
"""Local check that a coding harness can use a model through an OpenAI-compatible endpoint and solve BUG-01.

Each harness runs from a throwaway HOME / config folder, so your own harness settings are never touched.
The key is read from the SOKET_API_KEY environment variable (or --env-file) and never printed.

  SOKET_API_KEY=... python3 scripts/harness_probe.py pi hermes goose
  python3 scripts/harness_probe.py --env-file ~/Desktop/eval-bench/.env omp opencode
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "https://api.soket.ai/v1"
MODEL = "GLM-5.3"
PROMPT = "The `add` function in this project is wrong and tests fail.\nFix the implementation so `cargo test` passes.\nDo not change test code unless absolutely necessary.\n"
KEY = ""


def models_json():
    return json.dumps({"providers": {"soket": {"baseUrl": BASE, "api": "openai-completions", "apiKey": "$SOKET_API_KEY",
                                               "models": [{"id": MODEL}]}}})


def setup_pi(home, work):
    agent = home / "pi-agent"; agent.mkdir()
    (agent / "models.json").write_text(models_json())
    return [os.environ.get("PI_BIN", "pi"), "--print", PROMPT, "--provider", "soket", "--model", MODEL, "--no-session"], {"PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1"}


def setup_omp(home, work):
    agent = home / "omp-agent"; agent.mkdir()
    # OMP does not expand "$NAME" in apiKey (it sent the literal text and got 401), so write the key itself into the
    # throwaway config. In a pod this file is written at start-up from the secret, never baked in.
    (agent / "models.json").write_text(models_json().replace("$SOKET_API_KEY", KEY))
    sess = home / "omp-sessions"; sess.mkdir()
    return ["omp", "--print", "--auto-approve", "--no-title", "--session-dir", str(sess), "--cwd", str(work),
            "--model", f"soket/{MODEL}", PROMPT], {"PI_CODING_AGENT_DIR": str(agent)}


def setup_hermes(home, work):
    h = home / "hermes-home"; h.mkdir()
    (h / "config.yaml").write_text(f"model:\n  default: {MODEL}\n  provider: custom\n  base_url: {BASE}\n  api_key: \"${{SOKET_API_KEY}}\"\n")
    return ["hermes", "-z", PROMPT, "--yolo", "--ignore-rules", "--provider", "custom", "-m", MODEL, "--in", str(work)], {"HERMES_HOME": str(h), "TERMINAL_CWD": str(work)}


def setup_goose(home, work):
    root = home / "goose-root"; root.mkdir()
    return ["goose", "run", "-t", PROMPT, "-q", "--provider", "openai", "--model", MODEL, "--no-session", "--with-builtin", "developer"], {
        "GOOSE_PATH_ROOT": str(root), "GOOSE_DISABLE_KEYRING": "1", "GOOSE_MODE": "auto", "GOOSE_PROVIDER": "openai",
        "OPENAI_HOST": "https://api.soket.ai", "OPENAI_BASE_PATH": "v1/chat/completions", "OPENAI_API_KEY": KEY}


def setup_opencode(home, work):
    cfg = {"$schema": "https://opencode.ai/config.json", "provider": {"soket": {"npm": "@ai-sdk/openai-compatible", "name": "Soket",
           "options": {"baseURL": BASE, "apiKey": "{env:SOKET_API_KEY}"}, "models": {MODEL: {"name": MODEL}}}}}
    return ["opencode", "run", "--standalone", "--auto", "--format", "json", "--model", f"soket/{MODEL}", PROMPT], {
        "OPENCODE_CONFIG_CONTENT": json.dumps(cfg), "XDG_CONFIG_HOME": str(home / "xdg-config"),
        "XDG_DATA_HOME": str(home / "xdg-data"), "XDG_CACHE_HOME": str(home / "xdg-cache")}


def opencode_style_config():
    return json.dumps({"$schema": "https://opencode.ai/config.json", "provider": {"soket": {"npm": "@ai-sdk/openai-compatible", "name": "Soket",
                       "options": {"baseURL": BASE, "apiKey": "{env:SOKET_API_KEY}"}, "models": {MODEL: {"name": MODEL}}}}})


def setup_kilo(home, work):
    """Kilo is an OpenCode fork: same provider config, either env name accepted."""
    cfg = opencode_style_config()
    return ["kilo", "run", "--auto", "--format", "json", "--model", f"soket/{MODEL}", "--dir", str(work), PROMPT], {
        "KILO_CONFIG_CONTENT": cfg, "OPENCODE_CONFIG_CONTENT": cfg, "XDG_CONFIG_HOME": str(home / "xdg-config"),
        "XDG_DATA_HOME": str(home / "xdg-data"), "XDG_CACHE_HOME": str(home / "xdg-cache")}


def setup_cline(home, work):
    data, conf = home / "cline" / "data", home / "cline" / "config"
    data.mkdir(parents=True); conf.mkdir(parents=True)
    # `cline auth` stores the endpoint and key in the isolated folders (the key is on this command line only for an instant)
    a = subprocess.run(["cline", "auth", "-p", "openai", "-k", KEY, "-m", MODEL, "-b", BASE, "--data-dir", str(data), "--config", str(conf)],
                       capture_output=True, text=True, env={**os.environ, "HOME": str(home)}, stdin=subprocess.DEVNULL, timeout=60)
    print(f"  (cline auth exit {a.returncode}: {(a.stdout + a.stderr).replace(KEY, '***').strip()[-160:]})")
    return ["cline", "--json", "--data-dir", str(data), "--config", str(conf), "-P", "openai", "-m", MODEL, "--cwd", str(work), PROMPT], {}


def setup_dsh(home, work):
    """DeepSeek Harness: a patch overlay declares the custom provider (same shape the swebench runner uses)."""
    d = home / "dsh-home"; d.mkdir()
    patch = home / "bench.patch.yml"
    patch.write_text(f"""- id: agent-default-model
  config:
    provider: custom
    model: "{MODEL}"
- id: llm-pi-ai
  config:
    providers:
      custom:
        apiKeyEnv: BENCH_API_KEY
        api: openai-completions
        baseURL: "{BASE}"
        models:
          - id: "{MODEL}"
- id: bash-sandbox
  config:
    timeoutMs: 600000
- id: otel
  disabled: true
- id: session-telemetry-otel
  disabled: true
- id: tool-web
  disabled: true
""")
    return [os.environ.get("DSH_BIN", "dsh"), "--profile", "headless", "--patch", str(patch), PROMPT], {
        "DSH_HOME": str(d), "DSH_PERMISSION_MODE": "danger-full-access", "BENCH_API_KEY": KEY}


def setup_codex(home, work):
    """Expected to fail: Codex dropped the Chat Completions style and only speaks the Responses API, which Soket lacks (404)."""
    ch = home / "codex-home"; ch.mkdir()
    cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "--ignore-user-config", "--ignore-rules", "-C", str(work)]
    for kv in ('model_provider="soket"', 'model_providers.soket.name="Soket"', f'model_providers.soket.base_url="{BASE}"',
               'model_providers.soket.env_key="SOKET_API_KEY"', 'model_providers.soket.wire_api="responses"'):
        cmd += ["-c", kv]
    return cmd + ["-m", MODEL, PROMPT], {"CODEX_HOME": str(ch)}


SETUPS = {"dsh": setup_dsh, "kilo": setup_kilo, "cline": setup_cline, "pi": setup_pi, "omp": setup_omp, "hermes": setup_hermes, "goose": setup_goose, "opencode": setup_opencode, "codex": setup_codex}


def run(name, timeout):
    home = Path(tempfile.mkdtemp(prefix=f"probe-{name}-"))
    work = home / "task"
    shutil.copytree(REPO / "fixtures" / "mini-calc-buggy", work, ignore=shutil.ignore_patterns("target", ".git"))
    cmd, extra = SETUPS[name](home, work)
    # PWD must match the real working folder: OpenCode follows $PWD, and a stale one sent it into the real repo.
    env = {**os.environ, "HOME": str(home), "SOKET_API_KEY": KEY, "PWD": str(work), **extra}
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        out, code = p.stdout + "\n" + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, code = f"TIMEOUT after {timeout}s", "timeout"
    secs = round(time.time() - t, 1)
    v = subprocess.run(["cargo", "test", "-q"], cwd=work, capture_output=True, text=True, env={**os.environ, "CARGO_TARGET_DIR": str(home / "tgt")})
    print(f"[{name}] agent exit={code} in {secs}s | verifier {'PASS' if v.returncode == 0 else 'FAIL'}\n--- output tail:\n{out.replace(KEY, '***').strip()[-500:]}\n", flush=True)
    shutil.rmtree(home, ignore_errors=True)


def main():
    global KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("harness", nargs="+", choices=sorted(SETUPS))
    ap.add_argument("--env-file", help="a .env file holding SOKET_API_KEY")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    KEY = os.environ.get("SOKET_API_KEY", "")
    if a.env_file:
        for line in open(os.path.expanduser(a.env_file)):
            if line.startswith("SOKET_API_KEY="):
                KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not KEY:
        sys.exit("set SOKET_API_KEY or pass --env-file")
    for h in a.harness:
        run(h, a.timeout)


if __name__ == "__main__":
    main()
