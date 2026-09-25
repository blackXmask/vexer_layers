"""
API-key authentication for the Domain 6 orchestration gateway.

Disabled by default (local development / CI). Enable in orchestrator_config.json:
  "api": { "auth": { "enabled": true, "api_key": "...", "api_key_env": "VEXER_AGENT_API_KEY" } }

Send the key in the `X-API-Key` header. /health stays open for load-balancer probes.
"""
import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from .config import load_config

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def configured_api_key() -> Optional[str]:
    """Expected key, or None when authentication is disabled."""
    auth = load_config().get("api", {}).get("auth", {})
    if not auth.get("enabled", False):
        return None
    return os.getenv(auth.get("api_key_env") or "VEXER_AGENT_API_KEY") or auth.get("api_key")


def require_api_key(x_api_key: Optional[str] = Depends(_api_key_header)) -> None:
    """FastAPI dependency: 401 on missing/invalid key when auth is enabled."""
    expected = configured_api_key()
    if expected is None:
        return
    if not x_api_key or x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key (send X-API-Key header)"
        )
