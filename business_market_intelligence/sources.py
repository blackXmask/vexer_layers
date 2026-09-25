"""
Domain 7 Data Source Layer.

Collects raw items from configurable sources defined in config.json.
Deterministic by design so the module runs fully offline; real crawlers
(Domain 2 OSINT pipeline) can be dropped in later behind the same interface.
"""
import hashlib
import importlib
from typing import Any, List, Optional

from .config import load_config, render_template
from .models import RawItem, SourceType

# Person A's Domain 2 (OSINT) provider, injected at runtime by the host application.
_external_provider: Optional[Any] = None


def set_external_provider(provider: Any) -> None:
    """Injects a Domain 2 OSINT provider. Any object exposing
    ``collect(topic: str, limit: int = 10) -> list[dict]`` works (see INTEGRATION.md)."""
    global _external_provider
    _external_provider = provider


def get_external_provider() -> Optional[Any]:
    """Returns the injected provider, or the config-driven Domain 2 service if enabled."""
    if _external_provider is not None:
        return _external_provider
    cfg = load_config().get("sources", {}).get("domain2", {})
    if not cfg.get("enabled", False):
        return None
    try:
        module = importlib.import_module("osint_intelligence.service")
        return getattr(module, "OSINTIntelligenceService")()
    except (ImportError, AttributeError):
        return None

# Config source key -> SourceType mapping
_SOURCE_TYPES = {
    "news": SourceType.NEWS,
    "tenders": SourceType.TENDER,
    "social": SourceType.SOCIAL,
    "patents": SourceType.PATENT,
    "analyst": SourceType.ANALYST,
}


def collect_raw_items(topic: str) -> List[RawItem]:
    """Collects raw items from every enabled source in config.json.
    Titles/bodies may contain {{topic}} templates which are rendered here."""
    cfg = load_config().get("sources", {})
    context = {"topic": topic}
    items: List[RawItem] = []

    for source_key, source_cfg in cfg.items():
        if not isinstance(source_cfg, dict) or not source_cfg.get("enabled", True):
            continue
        source_type = _SOURCE_TYPES.get(source_key)
        if source_type is None:
            continue
        label = source_cfg.get("label", source_key)
        for index, raw in enumerate(source_cfg.get("items", [])):
            rendered = render_template(raw, context)
            item_id = _make_id(source_key, index, rendered.get("title", ""))
            items.append(RawItem(
                item_id=item_id,
                source_type=source_type,
                source_name=label,
                title=rendered.get("title", ""),
                body=rendered.get("body", ""),
                published=rendered.get("published", ""),
                url=rendered.get("url", ""),
                tags=[str(t).lower() for t in rendered.get("tags", [])]
            ))
    # Optional live feed from Person A's Domain 2 (OSINT). Falls back silently.
    provider = get_external_provider()
    if provider is not None:
        label = cfg.get("domain2", {}).get("label", "External OSINT (Domain 2)")
        try:
            for index, raw in enumerate(provider.collect(topic, limit=20) or []):
                item_id = _make_id("domain2", index, str(raw.get("title", "")))
                items.append(RawItem(
                    item_id=item_id,
                    source_type=SourceType.EXTERNAL,
                    source_name=str(raw.get("source_name") or label),
                    title=str(raw.get("title", "")),
                    body=str(raw.get("body", "")),
                    published=str(raw.get("published", "")),
                    url=str(raw.get("url", "")),
                    tags=[str(t).lower() for t in raw.get("tags", [])]
                ))
        except Exception:
            pass  # a broken external feed must never break static intelligence

    # Deterministic chronological ordering (newest first, stable tie-break by id)
    items.sort(key=lambda i: (i.published, i.item_id), reverse=True)
    return items


def _make_id(source_key: str, index: int, title: str) -> str:
    digest = hashlib.sha1(f"{source_key}:{index}:{title}".encode("utf-8")).hexdigest()[:8]
    return f"mbi_{source_key}_{digest}"
