FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    python3 \
    git jq \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @openai/codex@0.144.4 \
 && npm cache clean --force

COPY runner.py /app/runner.py

RUN useradd -m -s /bin/bash agent \
 && mkdir -p /work /home/agent/.codex \
 && chown -R agent:agent /work /app /home/agent

ENV HOME=/home/agent
ENV CODEX_HOME=/home/agent/.codex

USER agent
WORKDIR /work

ENTRYPOINT ["python3", "/app/runner.py"]
