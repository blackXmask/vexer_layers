"""
Central JSON configuration loader for Domain 8: Opportunity, Risk & Requirements Intelligence.

All runtime-tunable inputs live in `config.json` next to this file.
Integrated into the Vexer platform through the service facade in service.py
(consumed by Domain 6's ToolRegistry via a lazy adapter with fallback).

Environment variable overrides (highest precedence):
  VEXER_ORI_CONFIG_PATH  -> alternate config file path
"""
import json
import os
from functools import lru_cache
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json"
)


@lru_cache(maxsize=4)
def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Loads (and caches) the JSON configuration. Restart the service after editing."""
    config_path = path or os.getenv("VEXER_ORI_CONFIG_PATH", DEFAULT_CONFIG_PATH)
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
