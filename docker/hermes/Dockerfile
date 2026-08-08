FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1
ENV HOME=/root

ENV HERMES_HOME=/root/.hermes

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --retries 8 --timeout 180 \
        "hermes-agent[anthropic]==0.18.2" "pyyaml>=6"
RUN python3 -c "import yaml, importlib; importlib.import_module('hermes_cli')" \
    && hermes --version || true

COPY runner.py /app/runner.py

WORKDIR /work

ENTRYPOINT ["python3", "/app/runner.py"]
