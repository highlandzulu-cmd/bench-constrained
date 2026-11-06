# bench-constrained

Runs the 13-task coding benchmark as Kubernetes pods with **enforced CPU and memory limits** (for example 512 MiB and 1 CPU) and records pass or fail, peak memory, OOM kills, CPU throttling and wall-clock time for each run.

Each run is one Job with one pod. The pod holds the harness (Loop), the task's fixture project and the verifier, so everything counts against the same limit ("whole" mode). Requests equal limits, so the pod is Guaranteed QoS and the limits are real.

## What exists and what has been tested

| Piece | Status |
| --- | --- |
| `config/tasks.yaml`: the 13 tasks with exact prompts and verifiers | done, generated from the task files |
| All 13 verifiers fail on an untouched fixture | tested locally |
| Verifiers pass with a reference fix | tested for 8 of 13 (`solutions/`; `REF-01` and `WEB-REF-01` are trivial fixes because those verifiers only check a function exists). The 5 website tasks have no reference fix yet. |
| In-pod runner (`bench/podrun.py`): copy fixture, run agent, run verifier, read cgroup counters | tested locally in verify mode; cgroup parts tested against fake files |
| Pod/Job definition (`bench/manifests.py`) | unit tested: 512Mi / 1 CPU, requests = limits, locked-down, key by secret reference only |
| Status rules (oom / timeout / pass / fail / infra_error) and cluster polling | unit tested with fake API objects |
| Image (`image/Dockerfile`) | **written, never built** |
| Running on a real cluster, real OOM kills, real throttling | **not done**: needs the steps below |
| Agent mode with the model | **not run**: needs the image and the key secret |

Not built yet: token and cost metrics, the relative memory sweep, replay mode, charts, sandbox mode, heavier tasks.

## Deliberate differences from the original runner

- The website verifiers originally ran `npm install` first. Here `node_modules` is baked into the image and that step is dropped (no network, no install memory spike).
- Loop is started with `--permission-mode full-access --no-context-files --no-web` (the pod is the sandbox), the flags commonly used for unattended runs.
- The verifier timeout is 300 s (was 120 s) because a 0.5 CPU pod is slow.
- `max_turns` and `max_tokens` in the task files are not enforced (they were not before either); only the per-task timeout is.

## Fastest path: no push, no image (proves the limits are real)

From the machine holding the repo (you type the headnode password yourself):
```bash
rsync -av --exclude .venv --exclude results --exclude .git --exclude __pycache__ --exclude 'fixtures/*/node_modules' \
  ~/Desktop/bench-constrained/ <you>@<headnode>:~/bench-constrained/
```
Then on the headnode: `cd ~/bench-constrained && scripts/quickstart.sh`. It runs the read-only `preflight`, creates the
`bench-constrained` namespace and quota if missing, checks the pod definition against the cluster, and runs `bench selftest
--stock-image`: the four limit checks on the public `python:3.12-slim` image with this runner mounted from a ConfigMap.
The real 13 tasks need the task image (Rust, Node, Loop); see the next section.

## Image path (needed for the real tasks)

1. On the Mac, publish the repo; GitHub builds the image (about 10 minutes) and publishes `ghcr.io/highlandzulu-cmd/bench-tasks:0.1`:
   `gh repo create highlandzulu-cmd/bench-constrained --public --source=. --remote=origin --push`, then `gh run watch`.
   If the cluster cannot pull the image, set the package to public in GitHub (Packages, bench-tasks, settings).
2. On the headnode: `git clone https://github.com/highlandzulu-cmd/bench-constrained && cd bench-constrained && scripts/quickstart.sh`.
   It sets up Python, creates the namespace if missing, checks the pod definition against the cluster, runs `bench selftest`, then a no-model smoke run.

## Run it on the cluster (step by step)

All commands run on a machine that can reach the cluster (the headnode). Nothing here touches any namespace except `bench-constrained`.

```bash
# 0. One time, by someone with cluster-admin (creates the namespace and a quota):
kubectl apply -f deploy/namespace.yaml

# 1. The model key, typed by you on the headnode (never paste it into chat or a file in the repo):
kubectl -n bench-constrained create secret generic bench-api-key --from-literal=api-key="$SOKET_API_KEY"

# 2. Build and push the image, then set `image:` in config/settings.yaml
docker build --platform linux/amd64 -f image/Dockerfile -t <registry>/bench-tasks:0.1 .
docker push <registry>/bench-tasks:0.1

# 3. Python environment for the runner
python3 -m venv .venv && .venv/bin/pip install -e .

# 4. Check the pod definition against the cluster without creating anything
.venv/bin/bench manifest --task BUG-01 --profile tiny | kubectl apply --dry-run=server -f -

# 5. Prove the limits are real (OOM, child OOM, timeout, CPU 0.5 vs 1). Do this before trusting any result.
.venv/bin/bench selftest

# 6. Smoke test with no model: applies the reference fix and runs the verifier inside a 512Mi / 1 CPU pod
.venv/bin/bench run --profile tiny --mode verify --tasks BUG-01,WEB-BUG-01,TOOL-01 --repeats 1

# 7. The real thing: all 13 tasks with the model, 512 MiB and 1 CPU, 3 repeats
.venv/bin/bench run --profile tiny --repeats 3

# Other profiles are comma separated: --profile tiny,starve_384,starve_256
.venv/bin/bench summary <run-id>
.venv/bin/bench clean            # deletes only Jobs labelled app=bench-constrained
```

Results land in `results/<run-id>/<task>/<profile>/<repeat>/` as `result.json`, `logs.txt` and the `job.yaml` that was applied.

## Reading a result

`status` is one of `pass`, `fail`, `oom`, `timeout`, `infra_error`. `oom` wins whenever any OOM kill happened, including a child process killed inside a container that survived (`oom_kills_inside`, from the cgroup `memory.events` counter). `infra_error` (image pull, never scheduled, our own crash) is reported but excluded from the pass rate. Peak memory is the cgroup's `memory.peak`, or a 0.5 s sample of `memory.current` where the kernel has no `memory.peak`; it includes page cache, so it can read higher than the memory a task strictly needs.

## Tests

```bash
.venv/bin/python -m pytest -q     # 49 tests, about 20 s; needs cargo and node on the machine
```

## Where pods may run

`config/settings.yaml` ships with `allowed_nodes: []`, and `bench run`, `bench selftest` and `bench preflight` refuse to start
until nodes are named. Copy `config/settings.local.yaml.example` to `config/settings.local.yaml` (not committed) and list the
nodes you are cleared to use. Run `bench preflight` first: it is read-only and shows who else is on those nodes.

## Harnesses

`bench run --harness <name>` picks the agent under test (default `agent.harness` in `config/settings.yaml`).

| Harness | Version in the image | How it reaches the model |
| --- | --- | --- |
| `loop` | 0.6.0 | directly (OpenAI-compatible chat API) |
| `dsh` (DeepSeek Harness) | 0.2.2-rc.1 | directly, through a patch overlay naming the custom provider |
| `codex` | 0.166.0 | through the translator service: Codex only speaks the Responses API, which the model endpoint lacks |
| `pi` | 1.5.0 | directly, through a `models.json` naming the OpenAI-compatible endpoint |

Each was checked on BUG-01 against the model before being added (`scripts/harness_probe.py`, `scripts/codex_via_litellm_probe.py`).
Codex runs without its own sandbox because the pod is the sandbox; its tool calls and the translator never see the model key
(the translator has it, via a Secret).

### Running Codex: deploy the translator once

```bash
# 1. a random access key that only the translator and Codex pods share (typed/generated on the headnode, never pasted anywhere)
kubectl -n bench-constrained create secret generic bench-translator-key --from-literal=key="$(openssl rand -hex 24)"
# 2. the service (reads config/settings.local.yaml for the allowed nodes; refuses to run without it)
.venv/bin/bench translator-manifest | kubectl apply -f -
kubectl -n bench-constrained rollout status deploy/bench-translator
# 3. run
.venv/bin/bench run --harness codex --profile tiny --tasks BUG-01 --repeats 1 --code-from-configmap
```

The translator pod is outside the benchmark pods, so its memory does not count against the 512 MiB. It is labelled
`app=bench-translator`, so `bench clean` never removes it; delete it with
`.venv/bin/bench translator-manifest | kubectl delete -f -`.
