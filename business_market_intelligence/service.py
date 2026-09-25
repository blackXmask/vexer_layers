"""
Domain 7 Service Facade — the single entry point for Business & Market Intelligence.

Used in-process by Domain 6's ToolRegistry adapter, or remotely through api.py.
No dependency on agent_orchestrator (one-way integration, no cycles).
"""
import uuid
from typing import List, Optional

from .config import load_config
from .models import (
    CompetitorProfile,
    IntelligenceReport,
    MarketSegment,
    MarketSignal,
    Trend,
)
from .sources import collect_raw_items
from .analytics import (
    build_signals,
    detect_trends,
    score_competitors,
    score_segments,
)


class MarketIntelligenceService:
    """Stateless facade over source collection + analytics. Thread-safe."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            load_config(config_path)  # validates early; cached by loader

    def health(self) -> dict:
        cfg = load_config()
        enabled = [k for k, v in cfg.get("sources", {}).items()
                   if isinstance(v, dict) and v.get("enabled", True)]
        return {
            "status": "HEALTHY",
            "domain": cfg.get("domain"),
            "version": cfg.get("version"),
            "enabled_sources": enabled
        }

    def get_signals(self, topic: str, limit: Optional[int] = None) -> List[MarketSignal]:
        items = collect_raw_items(topic)
        signals = build_signals(items, topic)
        if limit is None:
            limit = int(load_config().get("analytics", {}).get("default_signal_limit", 10))
        return signals[:max(limit, 0)]

    def get_trends(self, topic: str) -> List[Trend]:
        items = collect_raw_items(topic)
        return detect_trends(items)

    def get_competitors(self, topic: str) -> List[CompetitorProfile]:
        items = collect_raw_items(topic)
        return score_competitors(items)

    def get_segments(self) -> List[MarketSegment]:
        return score_segments()

    def build_report(self, topic: str) -> IntelligenceReport:
        """Full intelligence report: signals + trends + competitors + segments,
        with opportunities/threats extraction and provenance citations."""
        report_cfg = load_config().get("report", {})
        max_signals = int(report_cfg.get("max_signals", 8))

        items = collect_raw_items(topic)
        signals = build_signals(items, topic)[:max_signals]
        trends = detect_trends(items)
        competitors = score_competitors(items)
        segments = score_segments()

        positive = [s for s in signals if s.sentiment.value == "POSITIVE"]
        negative = [s for s in signals if s.sentiment.value == "NEGATIVE"]
        high_threats = [c for c in competitors if c.threat_level == "HIGH"]
        spikes = [t for t in trends if t.is_spike]

        opportunities = [f"{s.title} [{s.source}]" for s in positive[:3]]
        if not opportunities and signals:
            opportunities = [f"Top signal: {signals[0].title} [{signals[0].source}]"]

        threats = [f"{s.title} [{s.source}]" for s in negative[:2]]
        threats += [f"High-threat competitor: {c.name} (score {c.threat_score})" for c in high_threats]
        threats += [f"Volume spike on '{t.topic}' (+{t.momentum:.0%} momentum)" for t in spikes[:2]]

        top_trend = trends[0] if trends else None
        summary = (
            f"Analyzed {len(signals)} signals for '{topic}' across "
            f"{len({s.source for s in signals})} sources. "
            + (f"Leading trend: {top_trend.topic} ({top_trend.direction.value}, volume {top_trend.volume}). "
               if top_trend else "")
            + f"Sentiment: {len(positive)} positive / {len(negative)} negative. "
            + (f"Highest-threat competitor: {competitors[0].name}." if competitors else "")
        )

        base_conf = float(report_cfg.get("base_confidence", 0.9))
        boost = float(report_cfg.get("per_signal_confidence_boost", 0.01))
        confidence = round(min(base_conf + len(signals) * boost, 0.99), 3)

        citations = sorted({s.source for s in signals} | {s.url for s in signals if s.url})

        return IntelligenceReport(
            report_id=f"mbi_{uuid.uuid4().hex[:8]}",
            topic=topic,
            executive_summary=summary,
            signals=signals,
            trends=trends,
            competitors=competitors,
            segments=segments,
            opportunities=opportunities,
            threats=threats,
            confidence=confidence,
            citations=citations
        )
