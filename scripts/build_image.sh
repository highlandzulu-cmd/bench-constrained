#!/usr/bin/env bash
# Build the task image on the headnode (docker is installed there) and test it at 512 MiB / 1 CPU
# BEFORE anything is pushed. Usage:
#   IMAGE=docker.io/<you>/bench-tasks:0.1 scripts/build_image.sh            # build + local test
#   IMAGE=docker.io/<you>/bench-tasks:0.1 scripts/build_image.sh --push     # ...then push (needs `docker login` first)
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:?set IMAGE, for example IMAGE=docker.io/yourname/bench-tasks:0.1}"
DOCKER="${DOCKER:-docker}"   # use DOCKER="sudo docker" if your user is not in the docker group but may use sudo

echo "== build $IMAGE (linux/amd64; the first build downloads Rust, Node and Loop: several minutes)"
$DOCKER build --platform linux/amd64 -f image/Dockerfile -t "$IMAGE" .

echo "== the tools are in the image"
$DOCKER run --rm "$IMAGE" sh -c 'cargo --version; cargo clippy --version; node --version; python3 --version; /opt/harness/bin/loop --version'

# Same limits as the benchmark pod: 512 MiB, no swap, 1 CPU, read-only root, non-root user.
run_task() {   # $1 = task id; runs the verifier (with the reference fix when one exists)
  local work; work="$(mktemp -d)"; chmod 777 "$work"
  $DOCKER run --rm --platform linux/amd64 --memory=512m --memory-swap=512m --cpus=1 --pids-limit=512 \
    --read-only --tmpfs /tmp:size=256m --cap-drop=ALL --security-opt no-new-privileges --user 10001:10001 \
    -v "$work:/work" -e HOME=/work/home -e CARGO_HOME=/work/cargo -e BENCH_TASK="$1" -e BENCH_MODE=verify \
    -e BENCH_PROGRESS_EVERY_S=999 "$IMAGE" python3 -m bench.podrun \
    | grep '^BENCH_RESULT ' | python3 -c 'import json,sys; r=json.loads(sys.stdin.readline().split(" ",1)[1]); print("%-12s %-5s verifier_exit=%s solution_applied=%s peak=%s MiB wall=%ss %s" % (r["task_id"], r["in_pod_status"], r["verifier_exit_code"], r.get("solution_applied"), r.get("peak_mem_mib"), r["wall_clock_s"], r.get("error","")[-200:]))'
  rm -rf "$work"
}
echo "== real tasks inside a 512 MiB / 1 CPU container (a task with a reference fix should say pass)"
run_task BUG-01        # Rust: cargo test
run_task TOOL-01       # Rust: cargo test + clippy
run_task WEB-BUG-01    # Node: node --test
run_task FEAT-02       # website: jsdom (no reference fix, so 'fail' proves the verifier runs)

if [ "${1:-}" = "--push" ]; then
  echo "== push $IMAGE"
  $DOCKER push "$IMAGE"
  echo "Now set  image: $IMAGE  in config/settings.yaml"
else
  echo "== not pushed (add --push after 'docker login')"
fi
