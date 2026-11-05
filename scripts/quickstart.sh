#!/usr/bin/env bash
# Run on a machine with kubectl access to the cluster, from inside the repo. Stops at the first problem.
# Needs config/settings.local.yaml (copy the .example file and name your nodes).
#   cd ~/bench-constrained && scripts/quickstart.sh      (copy the folder there with rsync; see README)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/5 python environment"
if [ ! -d .venv ]; then
  python3 -m venv .venv 2>/dev/null || python3 -m virtualenv .venv 2>/dev/null || virtualenv .venv
fi
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -e .

echo "== 1b/5 preflight: where pods may run and who is already there (read-only)"
.venv/bin/bench preflight --profile tiny || { echo "preflight says do not run; fix that first"; exit 1; }

echo "== 2/5 namespace and quota (only the bench-constrained namespace is ever touched)"
if kubectl get namespace bench-constrained >/dev/null 2>&1; then
  echo "namespace exists"
else
  kubectl apply -f deploy/namespace.yaml
fi

echo "== 3/5 pod definition accepted by the cluster (nothing is created)"
.venv/bin/bench manifest --task BUG-01 --profile tiny --mode verify | kubectl apply --dry-run=server -f -

echo "== 4/5 selftest: are the limits real? (OOM, child OOM, timeout, CPU 0.5 vs 1)"
echo "   (runs on the public python:3.12-slim image; no custom image needed)"
.venv/bin/bench selftest --stock-image

echo "== 5/5 smoke run of real tasks, no model (needs the task image; skipped unless BENCH_IMAGE_READY=1)"
if [ "${BENCH_IMAGE_READY:-0}" = "1" ]; then
  .venv/bin/bench run --profile tiny --mode verify --tasks BUG-01,WEB-BUG-01,TOOL-01 --repeats 1
else
  echo "   skipped: the real tasks need rust, node and loop in the image (see README)"
fi

echo
echo "Done. For the real run, first create the key (type it yourself):"
echo "  kubectl -n bench-constrained create secret generic bench-api-key --from-literal=api-key=\"\$SOKET_API_KEY\""
echo "then:  .venv/bin/bench run --profile tiny --repeats 3"
