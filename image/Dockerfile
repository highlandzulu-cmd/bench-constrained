# One image holds everything a task needs: Rust + clippy, Node (with the website fixture's
# node_modules already installed), the Loop binary, the 13 fixtures, and this runner.
# Build from the repo root (linux/amd64; for arm64 nodes build again with LOOP_TARGET=aarch64-unknown-linux-gnu):
#   docker build --platform linux/amd64 -f image/Dockerfile -t <registry>/bench-tasks:0.1 .
# Built by .github/workflows/image.yml on every push.
# The pods run as user 10001: it needs a passwd entry (the `useradd` below), otherwise Node's os.userInfo() throws ENOENT
# and programs such as DeepSeek Harness exit silently.
FROM rust:1-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/highlandzulu-cmd/bench-constrained"

ARG LOOP_VERSION=v0.6.0
ARG LOOP_TARGET=x86_64-unknown-linux-gnu

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-yaml curl ca-certificates perl \
 && rm -rf /var/lib/apt/lists/* \
 && rustup component add clippy \
 && useradd -u 10001 -U -M -d /work/home -s /bin/sh bench \
 && test "$(getent passwd 10001 | cut -d: -f1)" = "bench"

# Node 24: DeepSeek Harness 0.2.2-rc.1 needs >= 24.9 (on Node 20 it starts, prints nothing and exits 0). The website verifiers (node:test, jsdom) run fine on it.
COPY --from=node:24-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# Loop (glibc release, checksum verified)
RUN mkdir -p /opt/harness/bin \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz" -o /tmp/loop.tgz \
 && curl -fsSL "https://github.com/soketlabs/loop/releases/download/${LOOP_VERSION}/loop-${LOOP_TARGET}.tar.gz.sha256" -o /tmp/loop.sha256 \
 && echo "$(cut -d' ' -f1 /tmp/loop.sha256)  /tmp/loop.tgz" | sha256sum -c - \
 && tar -xzf /tmp/loop.tgz --strip-components=1 -C /opt/harness/bin \
 && chmod +x /opt/harness/bin/loop \
 && rm /tmp/loop.tgz /tmp/loop.sha256

# Harnesses under test besides Loop. Versions are pinned; DeepSeek Harness, Codex and Pi are the newest releases as of 2026-11-06 (Pi needs Node >= 22.19).
RUN npm install -g --no-audit --no-fund @deepseek-ai/dsh@0.2.2-rc.1 @openai/codex@0.166.0 @earendil-works/pi-coding-agent@1.5.0 \
 && test "$(dsh --version)" = "0.2.2-rc.1" && codex --version | grep -q "0.166.0" && test "$(pi --version)" = "1.5.0" \
 && node -e "const [a,b]=process.versions.node.split('.').map(Number); if (a<24 || (a===24 && b<9)) process.exit(1)"

# Goose (single binary, checksum from the GitHub release's published digest)
ARG GOOSE_VERSION=v1.57.0
ARG GOOSE_SHA256=10eb96cb6a159b6e1d38eeedbef6ab64cd28168aad91948f1fa5c34eefdf98e3
RUN curl -fsSL "https://github.com/block/goose/releases/download/${GOOSE_VERSION}/goose-x86_64-unknown-linux-gnu.tar.gz" -o /tmp/goose.tgz \
 && echo "${GOOSE_SHA256}  /tmp/goose.tgz" | sha256sum -c - \
 && tar -xzf /tmp/goose.tgz -C /usr/local/bin ./goose && chmod +x /usr/local/bin/goose && rm /tmp/goose.tgz \
 && goose --version | grep -q "1.57.0"

# Cline, in its own layer so a version bump re-downloads only this
RUN npm install -g --no-audit --no-fund cline@3.0.74 && test "$(cline --version)" = "3.0.74"

# Kilo Code, in its own layer
RUN npm install -g --no-audit --no-fund @kilocode/cli@7.8.27 && test "$(kilo --version)" = "7.8.27"

# Oh My Pi (OMP): standalone Linux binary from the GitHub release (it needs no Bun), checksum pinned
ARG OMP_VERSION=v18.17.0
ARG OMP_SHA256=a4ce6001ab1012ca835f4184d675437531bd1ac477a7ba8424ed0b5476e9e4cb
RUN curl -fsSL "https://github.com/can1357/oh-my-pi/releases/download/${OMP_VERSION}/omp-linux-x64" -o /usr/local/bin/omp \
 && echo "${OMP_SHA256}  /usr/local/bin/omp" | sha256sum -c - && chmod 755 /usr/local/bin/omp \
 && omp --version | grep -q "18.17.0"

# OpenCode 2.x (not on npm or the install script; the GitHub release ships the binary), checksum pinned
ARG OPENCODE_VERSION=v2.2.5
ARG OPENCODE_SHA256=09f44302c10fd036912f59c81fd1aef5df45dd06f649197f9dfc9bbe2a79614e
RUN curl -fsSL "https://github.com/anomalyco/opencode/releases/download/${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" -o /tmp/oc.tgz \
 && echo "${OPENCODE_SHA256}  /tmp/oc.tgz" | sha256sum -c - \
 && tar -xzf /tmp/oc.tgz -C /usr/local/bin && chmod 755 /usr/local/bin/opencode && rm /tmp/oc.tgz \
 && opencode --version | grep -q "2.2.5"

# Hermes Agent (Python, installed with uv into /opt so uid 10001 can read it)
ARG UV_VERSION=0.13.5
ENV UV_TOOL_DIR=/opt/hermes/tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/hermes/python UV_LINK_MODE=copy
RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" | tar -xz --strip-components=1 -C /usr/local/bin \
 && uv tool install --python 3.12 "https://github.com/NousResearch/hermes-agent/archive/refs/tags/v0.23.0.tar.gz" \
 && chmod -R a+rX /opt/hermes && hermes --version | head -1

# Fixtures; the website one gets its dependencies installed now so no verifier needs network.
COPY fixtures /opt/fixtures
RUN cd /opt/fixtures/dark-mode-site && npm ci --no-audit --no-fund

# The runner and its config (tasks.yaml is the 13 tasks)
COPY bench /opt/bench/bench
COPY config /opt/bench/config
COPY solutions /opt/bench/solutions
WORKDIR /opt/bench
ENV PYTHONPATH=/opt/bench PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
