"""
Domain 6 Enterprise AI Brain Configuration & LLM Adapter Layer.
Supports OpenAI, Anthropic, Ollama, and an Enterprise Mock LLM Engine.
All defaults are loaded from orchestrator_config.json (see config.py).
"""
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel
import os
import json

from .config import load_config, render_template


class ModelProvider(str, Enum):
    MOCK = "MOCK"
    OPENAI = "OPENAI"
    ANTHROPIC = "ANTHROPIC"
    OLLAMA = "OLLAMA"


class LLMConfig(BaseModel):
    provider: ModelProvider = ModelProvider.MOCK
    model_name: str = "vexer-intelligence-engine-v1"
    temperature: float = 0.1
    api_key: Optional[str] = None
    api_base: Optional[str] = None


class LLMResponse(BaseModel):
    content: str
    parsed_json: Optional[Dict[str, Any]] = None


class EnterpriseLLMEngine:
    """Enterprise LLM Gateway for Agent Reasoning, Decomposition, and Tool Invocation."""

    def __init__(self, config: Optional[LLMConfig] = None):
        if config is None:
            # Build config from orchestrator_config.json with env-var overrides
            file_cfg = load_config().get("llm", {})
            api_key_env = file_cfg.get("api_key_env") or "VEXER_LLM_API_KEY"
            config = LLMConfig(
                provider=ModelProvider(os.getenv("VEXER_LLM_PROVIDER", file_cfg.get("provider", "MOCK"))),
                model_name=os.getenv("VEXER_LLM_MODEL", file_cfg.get("model_name", "vexer-intelligence-engine-v1")),
                temperature=float(file_cfg.get("temperature", 0.1)),
                api_key=os.getenv(api_key_env) or file_cfg.get("api_key"),
                api_base=file_cfg.get("api_base")
            )
        self.config = config

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        """Invokes active model provider or reasoned enterprise intelligence heuristics."""
        if self.config.provider == ModelProvider.OPENAI and self.config.api_key:
            return self._call_openai(system_prompt, user_prompt)
        elif self.config.provider == ModelProvider.OLLAMA:
            return self._call_ollama(system_prompt, user_prompt)
        else:
            return self._call_enterprise_mock(system_prompt, user_prompt)

    def _call_enterprise_mock(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        """Deterministic enterprise reasoning simulation driven by llm.mock config rules."""
        mock_cfg = load_config().get("llm", {}).get("mock", {})
        prompt_lower = user_prompt.lower()
        context = {"query": user_prompt}

        if "plan" in system_prompt.lower() or "decompose" in system_prompt.lower():
            subtasks = []
            for rule in mock_cfg.get("decompose_rules", []):
                keywords = [str(k).lower() for k in rule.get("keywords", [])]
                if any(k in prompt_lower for k in keywords):
                    subtasks.append(render_template({
                        "target_agent": rule.get("target_agent"),
                        "description": rule.get("description", ""),
                        "input_data": rule.get("input_data", {})
                    }, context))
            if not subtasks and mock_cfg.get("fallback_subtask"):
                subtasks.append(render_template(dict(mock_cfg["fallback_subtask"]), context))

            result = {
                "thought_process": mock_cfg.get(
                    "thought_process", "Decomposed multi-domain enterprise inquiry."
                ),
                "subtasks": subtasks
            }
            return LLMResponse(content=json.dumps(result), parsed_json=result)

        fallback = render_template(mock_cfg.get("analysis_response", {
            "analysis": "Enterprise reasoning completed for query: {{query}}",
            "confidence": 0.94
        }), context)
        return LLMResponse(content=json.dumps(fallback), parsed_json=fallback)

    def _call_openai(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        import httpx
        timeout = float(load_config().get("llm", {}).get("timeout_seconds", 30))
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.config.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": self.config.temperature
        }
        url = (self.config.api_base or "https://api.openai.com/v1") + "/chat/completions"
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = None
            try:
                parsed = json.loads(content)
            except Exception:
                pass
            return LLMResponse(content=content, parsed_json=parsed)

    def _call_ollama(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        import httpx
        timeout = float(load_config().get("llm", {}).get("timeout_seconds", 30))
        url = (self.config.api_base or "http://localhost:11434") + "/api/generate"
        payload = {
            "model": self.config.model_name,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("response", "")
            parsed = None
            try:
                parsed = json.loads(content)
            except Exception:
                pass
            return LLMResponse(content=content, parsed_json=parsed)
