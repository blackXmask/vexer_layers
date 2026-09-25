"""
Domain 7 Analytics Engine.

Deterministic, explainable scoring:
- Lexicon-based sentiment analysis
- Keyword-overlap relevance ranking
- Trend momentum / direction / spike detection
- Competitor threat scoring (base threat x market mentions)
- Segment attractiveness scoring
All weights, lexicons and thresholds come from config.json.
"""
import re
from typing import Dict, List, Optional, Tuple

from .config import load_config
from .models import (
    CompetitorProfile,
    MarketSegment,
    MarketSignal,
    RawItem,
    Sentiment,
    Trend,
    TrendDirection,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def score_sentiment(text: str) -> Tuple[Sentiment, float]:
    """Lexicon-based sentiment in [-1.0, 1.0]."""
    lex = load_config().get("sentiment", {})
    lower = text.lower()
    pos = sum(1 for k in lex.get("positive_keywords", []) if k in lower)
    neg = sum(1 for k in lex.get("negative_keywords", []) if k in lower)
    hits = pos + neg
    if hits == 0:
        return Sentiment.NEUTRAL, 0.0
    score = (pos - neg) / hits
    if score > 0.1:
        sentiment = Sentiment.POSITIVE
    elif score < -0.1:
        sentiment = Sentiment.NEGATIVE
    else:
        sentiment = Sentiment.NEUTRAL
    return sentiment, round(score, 3)


def relevance_score(topic: str, item: RawItem) -> float:
    """Keyword-overlap relevance in [0, 1]; tag matches add a boost."""
    topic_tokens = [t for t in _tokens(topic) if len(t) > 2]
    if not topic_tokens:
        return 0.5
    item_token_set = set(_tokens(item.title + " " + item.body)) | set(item.tags)
    overlap = sum(1 for t in topic_tokens if t in item_token_set)
    relevance = overlap / len(topic_tokens)
    if set(topic_tokens) & set(item.tags):
        relevance += 0.3
    return round(min(max(relevance, 0.05), 1.0), 3)


def build_signal(item: RawItem, topic: str) -> MarketSignal:
    text = f"{item.title}. {item.body}"
    sentiment, sentiment_score = score_sentiment(text)
    return MarketSignal(
        signal_id=item.item_id,
        source_type=item.source_type,
        source=item.source_name,
        title=item.title,
        summary=item.body,
        sentiment=sentiment,
        sentiment_score=sentiment_score,
        relevance=relevance_score(topic, item),
        tags=list(item.tags),
        published=item.published,
        url=item.url
    )


def build_signals(items: List[RawItem], topic: str) -> List[MarketSignal]:
    """Scores all items and returns signals above min_relevance, ranked."""
    an = load_config().get("analytics", {})
    min_relevance = float(an.get("min_relevance", 0.1))
    signals = [build_signal(i, topic) for i in items]
    signals = [s for s in signals if s.relevance >= min_relevance]
    signals.sort(key=lambda s: (s.relevance, s.published, s.signal_id), reverse=True)
    return signals


def detect_trends(items: List[RawItem]) -> List[Trend]:
    """Per-tag momentum: compares recent half vs older half of the timeline."""
    an = load_config().get("analytics", {})
    min_volume = int(an.get("trend_min_volume", 2))
    spike_threshold = float(an.get("spike_threshold", 1.6))

    # items are newest-first; older half = second half of the list
    tag_counts: Dict[str, List[int]] = {}  # tag -> [older, newer]
    half = max(len(items) // 2, 1)
    for position, item in enumerate(items):
        for tag in item.tags:
            bucket = tag_counts.setdefault(tag, [0, 0])
            bucket[0 if position >= half else 1] += 1

    trends: List[Trend] = []
    for tag, (older, newer) in sorted(tag_counts.items()):
        volume = older + newer
        momentum = (newer - older) / max(volume, 1)
        if momentum >= 0.25:
            direction = TrendDirection.UP
        elif momentum <= -0.25:
            direction = TrendDirection.DOWN
        else:
            direction = TrendDirection.STABLE
        older_share = older / max(volume, 1)
        recent_share = newer / max(volume, 1)
        is_spike = volume >= min_volume and older_share > 0 and (recent_share / older_share) >= spike_threshold
        trends.append(Trend(
            trend_id=f"trend_{tag}",
            topic=tag,
            volume=volume,
            momentum=round(momentum, 3),
            direction=direction,
            confidence=round(min(1.0, volume / max(min_volume, 1)), 3),
            is_spike=is_spike
        ))
    trends.sort(key=lambda t: (t.volume, t.momentum), reverse=True)
    return trends


def score_competitors(items: List[RawItem]) -> List[CompetitorProfile]:
    """Threat = weighted blend of configured base threat and observed mentions."""
    an = load_config().get("analytics", {}).get("competitor", {})
    mention_weight = float(an.get("mention_weight", 0.4))
    base_weight = float(an.get("base_threat_weight", 0.6))
    weight_sum = max(mention_weight + base_weight, 1e-9)

    corpus = [(i.title + " " + i.body).lower() for i in items]
    profiles: List[CompetitorProfile] = []
    for comp in load_config().get("competitors", []):
        name = comp.get("name", "")
        name_lower = name.lower()
        mentions = sum(1 for text in corpus if name_lower in text or name_lower.split()[0] in text)
        mention_score = min(mentions / 3.0, 1.0)
        threat = (base_weight * float(comp.get("base_threat", 0.5)) + mention_weight * mention_score) / weight_sum
        threat = round(min(max(threat, 0.0), 1.0), 3)
        if threat >= 0.7:
            level = "HIGH"
        elif threat >= 0.45:
            level = "MEDIUM"
        else:
            level = "LOW"
        profiles.append(CompetitorProfile(
            name=name,
            positioning=comp.get("positioning", ""),
            strengths=comp.get("strengths", []),
            weaknesses=comp.get("weaknesses", []),
            mention_count=mentions,
            threat_score=threat,
            threat_level=level
        ))
    profiles.sort(key=lambda p: p.threat_score, reverse=True)
    return profiles


def score_segments(topic: Optional[str] = None) -> List[MarketSegment]:
    """
    Attractiveness = weighted blend of normalized growth and SAM size.

    ``topic`` is now accepted. When supplied, segments are ranked by lexical relevance to that topic
    and anything with no overlap is **excluded**. Previously ``get_segments()`` took no topic and
    returned the identical three segments for every query, so a report about an unrelated subject
    still asserted a $12B TAM as if it had been derived for it.
    """
    an = load_config().get("analytics", {}).get("segment", {})
    growth_weight = float(an.get("growth_weight", 0.6))
    size_weight = float(an.get("size_weight", 0.4))

    segments = load_config().get("segments", [])
    max_growth = max((float(s.get("growth_rate", 0)) for s in segments), default=1.0) or 1.0
    max_sam = max((float(s.get("sam_usd", 0)) for s in segments), default=1.0) or 1.0

    scored: List[MarketSegment] = []
    for seg in segments:
        growth_norm = float(seg.get("growth_rate", 0)) / max_growth
        size_norm = float(seg.get("sam_usd", 0)) / max_sam
        attractiveness = round(growth_weight * growth_norm + size_weight * size_norm, 3)

        relevance: Optional[float] = None
        if topic:
            tokens = {t for t in _TOKEN_RE.findall(topic.lower()) if len(t) > 2}
            name_tokens = set(_TOKEN_RE.findall(str(seg.get("name", "")).lower()))
            overlap = tokens & name_tokens
            if not overlap:
                # This segment says nothing about the query; returning it would be padding.
                continue
            relevance = round(len(overlap) / max(len(name_tokens), 1), 3)

        scored.append(MarketSegment(
            name=seg.get("name", ""),
            tam_usd=int(seg.get("tam_usd", 0)),
            sam_usd=int(seg.get("sam_usd", 0)),
            growth_rate=float(seg.get("growth_rate", 0)),
            attractiveness_score=attractiveness,
            is_estimate=True,
            source=str(seg.get("source", "curated reference set (config)")),
            topic_relevance=relevance,
            # A curated figure is never a live measurement.
            data_status="FALLBACK" if relevance is None else "DEGRADED",
        ))
    if topic:
        scored.sort(key=lambda s: (s.topic_relevance or 0.0, s.attractiveness_score), reverse=True)
    else:
        scored.sort(key=lambda s: s.attractiveness_score, reverse=True)
    return scored

