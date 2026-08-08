FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

ARG PIP_INDEX_URL=https://pypi.org/simple

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    python3 python3-pip \
    git jq \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --timeout 120 --retries 5 -i "$PIP_INDEX_URL" \
    claude-agent-sdk==0.2.87 \
    anyio \
    mcp

COPY runner_core.py /app/runner_core.py
COPY runner.py /app/runner.py

RUN useradd -m -s /bin/bash agent \
 && mkdir -p /work /home/agent/.claude /home/agent/.config /home/agent/.npm \
 && chown -R agent:agent /work /app /home/agent

ENV HOME=/home/agent
USER agent
WORKDIR /work

ENTRYPOINT ["python3", "/app/runner.py"]
