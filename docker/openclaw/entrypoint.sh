#!/bin/bash
set -e

echo "=== OpenClaw Container Starting (skillmisevo openclaw-minimax) ==="

if [ ! -f /app/configs/config.yaml ]; then
    echo "ERROR: config.yaml not found at /app/configs/config.yaml"
    exit 1
fi

if [ -z "${MISEVOLVE_API_KEY:-}" ]; then
    echo "ERROR: env MISEVOLVE_API_KEY is empty"
    exit 1
fi

echo "Mapping config.yaml -> /root/.openclaw/openclaw.json..."
python3 /app/config_mapper.py
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to map configuration"
    exit 1
fi
echo "Configuration mapped successfully"

mkdir -p /root/.openclaw/agents/main/sessions
mkdir -p /root/.openclaw/skills

echo "Starting OpenClaw gateway (bind=loopback)..."
openclaw gateway --allow-unconfigured --bind loopback &
GATEWAY_PID=$!

echo "Waiting for OpenClaw gateway to be ready..."
sleep 6
if ! kill -0 $GATEWAY_PID 2>/dev/null; then
    echo "ERROR: OpenClaw gateway failed to start"
    exit 1
fi
echo "OpenClaw gateway started (PID: $GATEWAY_PID)"

echo "Starting API server on :8000..."
python3 /app/api_server.py &
API_PID=$!
echo "API server started (PID: $API_PID)"

wait -n
echo "A process has exited, shutting down..."
kill $GATEWAY_PID $API_PID 2>/dev/null || true
exit 1
