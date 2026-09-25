"""
Domain 7: Business & Market Intelligence — Data Models.
"""
from enum import Enum
from typing import Optional, Any, Dict, List
from pydantic import BaseModel, Field


class SourceType(str, Enum):
    NEWS = "NEWS"
    TENDER = "TENDER"
    SOCIAL = "SOCIAL"
    PATENT = "PATENT"
    ANALYST = "ANALYST"
    EXTERNAL = "EXTERNAL"   # live feed from Person A's Domain 2 (OSINT)


class Sentiment(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class TrendDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    STABLE = "STABLE"


class RawItem(BaseModel):
    """A raw collected item from one of the configured sources."""
    item_id: str
    source_type: SourceType
    source_name: str
    title: str
    body: str
    published: str
    url: str
    tags: List[str] = Field(default_factory=list)


class MarketSignal(BaseModel):
    """A scored, provenance-carrying signal ready for consumption."""
    signal_id: str
    source_type: SourceType
    source: str
    title: str
    summary: str
    sentiment: Sentiment = Sentiment.NEUTRAL
    sentiment_score: float = 0.0
    relevance: float = 0.0
    tags: List[str] = Field(default_factory=list)
    published: str = ""
    url: str = ""

    def to_domain6_osint(self) -> Dict[str, Any]:
        """Maps to the OSINT dict shape consumed by Domain 6's ToolRegistry
        (legacy keys preserved; Domain 7 enrichment fields added)."""
        return {
            "source": self.source,
            "headline": self.title,
            "timestamp": self.published[:10] if self.published else "",
            "credibility_score": round(0.7 + 0.3 * self.relevance, 3),
            # Domain 7 enrichment
            "signal_id": self.signal_id,
            "sentiment": self.sentiment.value,
            "sentiment_score": self.sentiment_score,
            "relevance": round(self.relevance, 3),
            "url": self.url
        }


class Trend(BaseModel):
    trend_id: str
    topic: str
    volume: int = 0
    momentum: float = 0.0
    direction: TrendDirection = TrendDirection.STABLE
    confidence: float = 0.0
    is_spike: bool = False


class CompetitorProfile(BaseModel):
    name: str
    positioning: str
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    mention_count: int = 0
    threat_score: float = 0.0
    threat_level: str = "LOW"  # LOW, MEDIUM, HIGH


class MarketSegment(BaseModel):
    """
    A market segment with sizing figures.

    ``is_estimate`` and ``source`` exist because the segment table is a **curated reference set**, not
    a measurement. The previous model returned these figures for *any* topic with no indication
    that they were static, so a report about an unrelated subject still claimed a $12B TAM. Sizing
    numbers without provenance read as findings; these fields make the gap explicit (§41, §52).
    """

    name: str
    tam_usd: int
    sam_usd: int
    growth_rate: float
    attractiveness_score: float = 0.0
    #: True while the figures come from the curated config rather than a measured source.
    is_estimate: bool = True
    source: str = "curated reference set (config)"
    #: Relevance to the queried topic, when the segment was filtered by one. None = not topic-scoped.
    topic_relevance: Optional[float] = None
    data_status: str = "UNAVAILABLE"


class IntelligenceReport(BaseModel):
    report_id: str
    topic: str
    executive_summary: str
    signals: List[MarketSignal] = Field(default_factory=list)
    trends: List[Trend] = Field(default_factory=list)
    competitors: List[CompetitorProfile] = Field(default_factory=list)
    segments: List[MarketSegment] = Field(default_factory=list)
    opportunities: List[str] = Field(default_factory=list)
    threats: List[str] = Field(default_factory=list)
    confidence: float = 0.0
    citations: List[str] = Field(default_factory=list)
