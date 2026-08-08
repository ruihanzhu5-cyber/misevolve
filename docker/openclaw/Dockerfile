FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/root
ENV NODE_ENV=production

RUN apt-get -o Acquire::Retries=8 update && apt-get -o Acquire::Retries=8 install -y \
    curl \
    git \
    ca-certificates \
    gnupg \
    python3 \
    python3-pip \
    jq \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version

RUN npm install -g openclaw@latest \
    && openclaw --version

RUN pip3 install --no-cache-dir fastapi uvicorn pyyaml aiofiles

RUN mkdir -p /root/.openclaw/workspace \
    && mkdir -p /root/.openclaw/agents/main/sessions \
    && mkdir -p /root/.openclaw/skills \
    && mkdir -p /app/configs

COPY config_mapper.py /app/config_mapper.py
COPY api_server.py /app/api_server.py
COPY entrypoint.sh /app/entrypoint.sh
COPY config.yaml /app/configs/config.yaml

RUN chmod +x /app/entrypoint.sh

EXPOSE 18789 8000

WORKDIR /app

HEALTHCHECK --interval=10s --timeout=10s --start-period=20s --retries=5 \
  CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
