"""Codex only speaks the OpenAI Responses API; Soket offers Chat Completions only. LiteLLM can translate between them.
Starts a local LiteLLM translator, runs Codex (its own sandbox on) against it on BUG-01, and checks the fix.

  LITELLM_BIN=/path/to/venv/bin/litellm CODEX_BIN=/path/to/codex ENV_FILE=~/path/.env python3 scripts/codex_via_litellm_probe.py openai

The model must be labelled `openai/...` AND carry use_chat_completions_api: true, otherwise LiteLLM forwards the
Responses call to Soket unchanged and gets a 404.
"""
import json, os, secrets, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
PROV = sys.argv[1]
KEY = os.environ.get("SOKET_API_KEY") or next(l.split("=",1)[1].strip().strip('"').strip("'") for l in open(os.path.expanduser(os.environ["ENV_FILE"])) if l.startswith("SOKET_API_KEY="))
MASTER = "sk-" + secrets.token_hex(16)
LL = os.environ.get("LITELLM_BIN", "litellm")      # a venv with: pip install "litellm[proxy]"
CODEX = os.environ.get("CODEX_BIN", "codex")      # e.g. npm install --prefix DIR @openai/codex@latest
home = Path(tempfile.mkdtemp(prefix="probe-codex-"))
cfg = home / "litellm.yaml"
cfg.write_text(f"model_list:\n  - model_name: GLM-5.3\n    litellm_params:\n      model: {PROV}/GLM-5.3\n      api_base: https://api.soket.ai/v1\n      api_key: os.environ/SOKET_API_KEY\n      use_chat_completions_api: true\nlitellm_settings:\n  drop_params: true\n")
port = 4011
proxy = subprocess.Popen([LL, "--config", str(cfg), "--port", str(port), "--host", "127.0.0.1"], env={**os.environ, "SOKET_API_KEY": KEY, "LITELLM_MASTER_KEY": MASTER}, stdout=open(home/"proxy.log","w"), stderr=subprocess.STDOUT)
try:
    for _ in range(60):
        time.sleep(2)
        try: urllib.request.urlopen(f"http://127.0.0.1:{port}/health/readiness", timeout=2); break
        except Exception: pass
    else: raise SystemExit("proxy did not start")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/responses", data=json.dumps({"model":"GLM-5.3","input":"Reply with exactly: ok","max_output_tokens":300}).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer "+MASTER})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read()); out = [c.get("text") for o in r.get("output",[]) for c in (o.get("content") or []) if isinstance(c,dict)]
        print(f"[{PROV}] POST /v1/responses -> 200, text:", out[:1])
    except Exception as e:
        print(f"[{PROV}] POST /v1/responses failed:", str(e)[:60], "|", (e.read().decode(errors="replace") if hasattr(e, "read") else "")[:420].replace(KEY, "***")); raise SystemExit
    work = home / "task"; shutil.copytree("fixtures/mini-calc-buggy", work, ignore=shutil.ignore_patterns("target", ".git"))
    ch = home / "codex-home"; ch.mkdir()
    prompt = "The `add` function in this project is wrong and tests fail.\nFix the implementation so `cargo test` passes.\nDo not change test code unless absolutely necessary.\n"
    cmd = [CODEX, "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "--ignore-user-config", "--ignore-rules", "-C", str(work), "-c", 'approval_policy="never"']
    for kv in ('model_provider="soket"', 'model_providers.soket.name="Soket via translator"', f'model_providers.soket.base_url="http://127.0.0.1:{port}/v1"', 'model_providers.soket.env_key="LOCAL_PROXY_KEY"', 'model_providers.soket.wire_api="responses"'):
        cmd += ["-c", kv]
    cmd += ["-m", "GLM-5.3", prompt]
    t = time.time()
    p = subprocess.run(cmd, cwd=work, env={**os.environ, "HOME": str(home), "CODEX_HOME": str(ch), "LOCAL_PROXY_KEY": MASTER, "PWD": str(work)}, capture_output=True, text=True, timeout=400, stdin=subprocess.DEVNULL)
    v = subprocess.run(["cargo","test","-q"], cwd=work, capture_output=True, text=True, env={**os.environ, "CARGO_TARGET_DIR": str(home/"tgt")})
    print(f"[codex via translator ({PROV}/, sandboxed)] exit={p.returncode} in {round(time.time()-t,1)}s | verifier {'PASS' if v.returncode==0 else 'FAIL'}")
    print("--- tail:", (p.stdout[-500:] + " || " + p.stderr[-300:]).replace(KEY,"***").replace(MASTER,"***").replace("\n"," | "))
finally:
    proxy.terminate(); shutil.rmtree(home, ignore_errors=True)
