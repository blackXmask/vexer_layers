"""
Central JSON configuration loader for Domain 6.

All runtime-tunable inputs live in `orchestrator_config.json` next to this file.
This sub-system is designed to be configured independently before integration
into the wider Vexer Enterprise Intelligence Platform.

Environment variable overrides (highest precedence):
  VEXER_CONFIG_PATH    -> alternate config file path
  VEXER_LLM_PROVIDER   -> llm.provider            (e.g. MOCK, OPENAI, OLLAMA)
  VEXER_LLM_MODEL      -> llm.model_name
  VEXER_LLM_API_KEY    -> read via llm.api_key_env
  VEXER_AGENT_DB       -> api.database_path
"""
import json
import os
from functools import lru_cache
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "orchestrator_config.json"
)


@lru_cache(maxsize=4)
def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Loads (and caches) the JSON configuration. Restart the service after editing."""
    config_path = path or os.getenv("VEXER_CONFIG_PATH", DEFAULT_CONFIG_PATH)
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def reload_config() -> None:
    """Clears the config cache (useful in tests or runtime config swaps)."""
    load_config.cache_clear()


def render_template(value: Any, context: Dict[str, Any]) -> Any:
    """Recursively substitutes {{placeholder}} tokens inside strings/dicts/lists."""
    if isinstance(value, str):
        rendered = value
        for key, replacement in context.items():
            rendered = rendered.replace("{{" + key + "}}", str(replacement))
        return rendered
    if isinstance(value, dict):
        return {k: render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(item, context) for item in value]
    return value


def agent_settings(role_value: str) -> Dict[str, Any]:
    """Returns the `agents.<role_lowercase>` settings block for an AgentRole value."""
    return load_config().get("agents", {}).get(role_value.lower(), {})


def agent_enabled(role_value: str) -> bool:
    """True unless the agent is explicitly disabled in config (`agents.<role>.enabled`)."""
    return bool(agent_settings(role_value).get("enabled", True))
