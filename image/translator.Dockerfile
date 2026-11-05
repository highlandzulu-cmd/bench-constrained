# The translator for Codex: LiteLLM, turning OpenAI Responses calls into Chat Completions calls for the model endpoint.
# Version 1.108.0 is the one the Codex test passed with. Built by .github/workflows/image.yml.
FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/highlandzulu-cmd/bench-constrained"
RUN pip install --no-cache-dir "litellm[proxy]==1.108.0" && useradd -u 10001 -m translator
USER 10001
ENV HOME=/home/translator PYTHONUNBUFFERED=1 LITELLM_TELEMETRY=False
EXPOSE 4000
CMD ["litellm", "--config", "/config/litellm.yaml", "--port", "4000", "--host", "0.0.0.0"]
