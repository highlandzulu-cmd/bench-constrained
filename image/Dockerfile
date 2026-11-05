# One image holds everything a task needs: Rust + clippy, Node (with the website fixture's
# node_modules already installed), the Loop binary, the 13 fixtures, and this runner.
# Build from the repo root (linux/amd64; for arm64 nodes build again with LOOP_TARGET=aarch64-unknown-linux-gnu):
#   docker build --platform linux/amd64 -f image/Dockerfile -t <registry>/bench-tasks:0.1 .
# Built by .github/workflows/image.yml on every push; not yet run in a pod.
FROM rust:1-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/highlandzulu-cmd/bench-constrained"

ARG LOOP_VERSION=v0.6.0
ARG LOOP_TARGET=x86_64-unknown-linux-gnu

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-yaml curl ca-certificates perl \
 && rm -rf /var/lib/apt/lists/* \
 && rustup component add clippy

# Node 20 (the website verifiers use node:test and jsdom)
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Loop (glibc release, checksum verified)
RUN mkdir -p /opt/harness/bin \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz" -o /tmp/loop.tgz \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz.sha256" -o /tmp/loop.sha256 \
 && echo "$(cut -d' ' -f1 /tmp/loop.sha256)  /tmp/loop.tgz" | sha256sum -c - \
 && tar -xzf /tmp/loop.tgz --strip-components=1 -C /opt/harness/bin \
 && chmod +x /opt/harness/bin/loop \
 && rm /tmp/loop.tgz /tmp/loop.sha256

# Fixtures; the website one gets its dependencies installed now so no verifier needs network.
COPY fixtures /opt/fixtures
RUN cd /opt/fixtures/dark-mode-site && npm ci --no-audit --no-fund

# The runner and its config (tasks.yaml is the 13 tasks)
COPY bench /opt/bench/bench
COPY config /opt/bench/config
COPY solutions /opt/bench/solutions
WORKDIR /opt/bench
ENV PYTHONPATH=/opt/bench PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
