"""
Domain 7: Business & Market Intelligence — Package Exports.
"""
from .config import load_config, reload_config
from .models import (
    CompetitorProfile,
    IntelligenceReport,
    MarketSegment,
    MarketSignal,
    RawItem,
    Sentiment,
    SourceType,
    Trend,
    TrendDirection,
)
from .sources import collect_raw_items, get_external_provider, set_external_provider
from .analytics import (
    build_signals,
    detect_trends,
    score_competitors,
    score_segments,
    score_sentiment,
)
from .service import MarketIntelligenceService

__all__ = [
    "load_config",
    "reload_config",
    "CompetitorProfile",
    "IntelligenceReport",
    "MarketSegment",
    "MarketSignal",
    "RawItem",
    "Sentiment",
    "SourceType",
    "Trend",
    "TrendDirection",
    "collect_raw_items",
    "set_external_provider",
    "get_external_provider",
    "build_signals",
    "detect_trends",
    "score_competitors",
    "score_segments",
    "score_sentiment",
    "MarketIntelligenceService",
]
