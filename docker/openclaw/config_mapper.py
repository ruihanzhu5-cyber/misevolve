"""配置映射器 — SkillMisevo-Gym openclaw-minimax cell.

Maps the container's non-secret /app/configs/config.yaml into the OpenClaw config
file /root/.openclaw/openclaw.json with a **backbone** provider (OpenAI-compatible /v1
endpoint). The backbone key is read from env MISEVOLVE_API_KEY at container start
(passed via `docker run -e MISEVOLVE_API_KEY=...`) and is NEVER stored in config.yaml
or baked into the image. Model and base URL likewise come from MISEVOLVE_MODEL /
MISEVOLVE_BASE_URL, so one image serves every backbone.

Adapted from the AgentHazard config-mapper (which mapped an OpenAI-completions
Idealab provider). Differences: provider=backbone, api=openai-completions, model and
endpoint sourced from env, gateway auth mode configurable (default none).
"""

import json
import os
from pathlib import Path

import yaml

CONFIG_YAML = "/app/configs/config.yaml"
OPENCLAW_JSON = "/root/.openclaw/openclaw.json"


def map_config():
    """将 config.yaml + env(MISEVOLVE_MODEL/BASE_URL/API_KEY) 映射为 openclaw.json"""

    print(f"Reading config from: {CONFIG_YAML}")
    with open(CONFIG_YAML, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    print(f"Loaded config: {list(config.keys())}")

    api_config = config.get("api", {})
    model_config = config.get("model", {})
    agent_config = config.get("agent", {})
    gateway_config = config.get("gateway", {})
    features = config.get("features", {})
    messages_config = config.get("messages", {})
    commands_config = config.get("commands", {})
    skills_config = config.get("skills", {})
    tailscale_config = config.get("tailscale", {})

    key_env = api_config.get("key_env", "MISEVOLVE_API_KEY")
    api_key = os.environ.get(key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"env {key_env} is empty — pass the backbone key via `docker run -e {key_env}=...`")

    base_url = os.environ.get("MISEVOLVE_BASE_URL", "").strip() or api_config.get("url", "")
    if not base_url:
        raise RuntimeError("no base URL: set env MISEVOLVE_BASE_URL or api.url in config.yaml")

    provider_name = model_config.get("provider_name", "backbone")
    model_id = os.environ.get("MISEVOLVE_MODEL", "").strip() or model_config.get("id", "")
    model_api = model_config.get("api", "anthropic-messages")

    openclaw_config = {
        "meta": {
            "lastTouchedVersion": "2026.7.1",
            "lastTouchedAt": "2026-07-15T00:00:00.000Z",
        },
        "browser": {
            "enabled": features.get("browser_enabled", False),
            "evaluateEnabled": features.get("browser_evaluate_enabled", False),
            "attachOnly": features.get("browser_attach_only", True),
        },
        "models": {
            "mode": "merge",
            "providers": {
                provider_name: {
                    "baseUrl": base_url,
                    "apiKey": api_key,
                    "api": model_api,
                    "models": [
                        {
                            "id": model_id,
                            "name": model_config.get("name", model_id),
                            "reasoning": model_config.get("reasoning", True),
                            "input": ["text"],
                            "contextWindow": model_config.get("context_window", 204800),
                            "maxTokens": model_config.get("max_tokens", 131072),
                            "cost": {
                                "input": 0,
                                "output": 0,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                            },
                        }
                    ],
                }
            },
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": f"{provider_name}/{model_id}",
                },
                "workspace": agent_config.get("workspace", "/root/.openclaw/workspace"),
                "compaction": {
                    "mode": agent_config.get("compaction_mode", "safeguard"),
                },
                "maxConcurrent": agent_config.get("max_concurrent", 4),
                "subagents": {
                    "maxConcurrent": agent_config.get("subagents_max_concurrent", 8),
                },
            }
        },
        "messages": {
            "ackReactionScope": messages_config.get("ack_reaction_scope", "group-mentions"),
        },
        "commands": {
            "native": commands_config.get("native", "auto"),
            "nativeSkills": commands_config.get("native_skills", "auto"),
        },
        "cron": {"enabled": features.get("cron_enabled", True)},
        "web": {"enabled": features.get("web_enabled", True)},
        "canvasHost": {"enabled": features.get("canvas_host_enabled", False)},
        "gateway": {
            "port": gateway_config.get("port", 18789),
            "mode": gateway_config.get("mode", "local"),
            "bind": gateway_config.get("bind", "lan"),
            "auth": {
                "mode": gateway_config.get("auth_mode", "none"),
                "token": gateway_config.get("auth_token", ""),
            },
            "tailscale": {
                "mode": tailscale_config.get("mode", "off"),
                "resetOnExit": tailscale_config.get("reset_on_exit", False),
            },
        },
        "skills": {
            "install": {
                "nodeManager": skills_config.get("node_manager", "npm"),
            }
        },
    }

    extra_plugins = [p.strip() for p in os.environ.get("OPENCLAW_EXTRA_PLUGINS", "").split(",") if p.strip()]
    if extra_plugins:
        openclaw_config["plugins"] = {"entries": {name: {"enabled": True} for name in extra_plugins}}
        print(f"Extra plugins enabled from OPENCLAW_EXTRA_PLUGINS: {', '.join(extra_plugins)}")

    openclaw_dir = Path(OPENCLAW_JSON).parent
    openclaw_dir.mkdir(parents=True, exist_ok=True)
    with open(OPENCLAW_JSON, "w", encoding="utf-8") as f:
        json.dump(openclaw_config, f, indent=2, ensure_ascii=False)

    print(f"✓ Configuration mapped successfully to: {OPENCLAW_JSON}")
    print("\n=== Generated Configuration Summary ===")
    print(f"Provider: {provider_name}")
    print(f"Model: {model_id} (api={model_api})")
    print(f"API URL: {base_url}")
    print(f"API key: <{len(api_key)} chars from env {key_env}>")
    print(
        f"Gateway: port={openclaw_config['gateway']['port']} "
        f"bind={openclaw_config['gateway']['bind']} "
        f"auth={openclaw_config['gateway']['auth']['mode']}"
    )
    print(f"Workspace: {agent_config.get('workspace', '/root/.openclaw/workspace')}")
    print("=" * 40)
    return True


if __name__ == "__main__":
    try:
        if not os.path.exists(CONFIG_YAML):
            print(f"ERROR: Config file not found: {CONFIG_YAML}")
            exit(1)
        map_config()
        print("\n✅ Configuration mapping completed successfully!")
        exit(0)
    except Exception as e:
        print("\n❌ ERROR: Failed to map configuration")
        print(f"Error details: {e}")
        import traceback

        traceback.print_exc()
        exit(1)
