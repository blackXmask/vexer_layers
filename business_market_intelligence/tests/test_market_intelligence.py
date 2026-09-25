"""
Comprehensive Test Suite for Domain 7: Business & Market Intelligence.
Validates:
1. Source collection (config-driven, provenance, templates)
2. Sentiment lexicon analysis
3. Signal relevance ranking & limits
4. Trend momentum / direction / spike detection
5. Competitor threat scoring
6. Segment attractiveness scoring
7. Full intelligence report generation
8. REST API endpoints
9. Domain 6 ToolRegistry integration (adapter + RBAC preserved)
"""
import pytest
from fastapi.testclient import TestClient

from business_market_intelligence import (
    MarketIntelligenceService,
    Sentiment,
    collect_raw_items,
    score_sentiment,
)
from business_market_intelligence.api import app

TOPIC = "sovereign AI defense tender"


@pytest.fixture()
def service():
    return MarketIntelligenceService()


def test_source_collection_provenance_and_templates():
    """All enabled sources yield items carrying full provenance fields."""
    items = collect_raw_items(TOPIC)
    assert len(items) >= 10
    for item in items:
        assert item.source_name
        assert item.title
        assert item.url.startswith("http")
        assert item.published
        assert item.tags
        assert item.item_id.startswith("mbi_")
    # newest-first ordering
    dates = [i.published for i in items]
    assert dates == sorted(dates, reverse=True)


def test_sentiment_lexicon_analysis():
    """Lexicon-based sentiment classifies positive/negative/neutral text."""
    pos, pos_score = score_sentiment("record growth and strong surge in adoption")
    neg, neg_score = score_sentiment("investigation penalty and shortage risk")
    neu, neu_score = score_sentiment("meeting scheduled for tuesday")
    assert pos == Sentiment.POSITIVE and pos_score > 0
    assert neg == Sentiment.NEGATIVE and neg_score < 0
    assert neu == Sentiment.NEUTRAL and neu_score == 0.0


def test_competitor_threat_scoring(service):
    """Threat scores are bounded, sorted, and level-consistent."""
    competitors = service.get_competitors(TOPIC)
    assert len(competitors) == 3
    scores = [c.threat_score for c in competitors]
    assert scores == sorted(scores, reverse=True)
    for c in competitors:
        assert 0.0 <= c.threat_score <= 1.0
        assert c.threat_level in {"LOW", "MEDIUM", "HIGH"}
        if c.threat_score >= 0.7:
            assert c.threat_level == "HIGH"
    # CompetitorCorp has the highest configured base threat
    assert competitors[0].name == "CompetitorCorp"


def test_segment_attractiveness_scoring(service):
    """Segments are scored in [0,1] and sorted by attractiveness."""
    segments = service.get_segments()
    assert len(segments) == 3
    scores = [s.attractiveness_score for s in segments]
    assert scores == sorted(scores, reverse=True)
    for s in segments:
        assert 0.0 <= s.attractiveness_score <= 1.0
        assert s.tam_usd >= s.sam_usd > 0
    assert segments[0].name == "Sovereign AI Platforms"


def test_intelligence_report_generation(service):
    """Report carries summary, opportunities, threats, confidence, citations."""
    report = service.build_report(TOPIC)
    assert report.report_id.startswith("mbi_")
    assert TOPIC in report.executive_summary
    assert len(report.signals) > 0
    assert len(report.trends) > 0
    assert len(report.competitors) > 0
    assert len(report.segments) > 0
    assert len(report.opportunities) > 0
    assert len(report.threats) > 0
    assert 0.0 < report.confidence <= 1.0
    assert len(report.citations) >= len({s.source for s in report.signals})


def test_rest_api_endpoints():
    """Verify Domain 7 REST gateway lifecycle."""
    client = TestClient(app)

    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "HEALTHY"

    r = client.get("/intelligence/signals", params={"topic": TOPIC, "limit": 4})
    assert r.status_code == 200
    signals = r.json()
    assert 0 < len(signals) <= 4
    assert "source" in signals[0] and "sentiment" in signals[0]

    r = client.get("/intelligence/trends", params={"topic": TOPIC})
    assert r.status_code == 200 and len(r.json()) > 0

    r = client.get("/intelligence/competitors", params={"topic": TOPIC})
    assert r.status_code == 200 and len(r.json()) == 3

    r = client.get("/intelligence/segments")
    assert r.status_code == 200 and len(r.json()) == 3

    r = client.post("/intelligence/report", json={"topic": TOPIC, "max_signals": 3})
    assert r.status_code == 200
    report = r.json()
    assert len(report["signals"]) <= 3
    assert report["confidence"] > 0


def test_domain6_toolregistry_integration():
    """Domain 6's ToolRegistry delegates OSINT to Domain 7 with RBAC preserved."""
    from agent_orchestrator.tools import ToolRegistry

    ToolRegistry.clear_audit_trail()

    # Authorized call -> delegated to Domain 7
    signals = ToolRegistry.query_osint_signals(TOPIC, caller_agent="MARKET_INTELLIGENCE")
    assert len(signals) > 0
    for s in signals:
        # legacy Domain 6 contract keys
        assert "source" in s and "headline" in s and "timestamp" in s
        # Domain 7 enrichment
        assert "sentiment" in s and "relevance" in s and "url" in s

    audit = ToolRegistry.get_audit_trail()
    assert audit[0].tool_name == "query_osint_signals"
    assert audit[0].arguments.get("provider") == "domain7"

    # RBAC still enforced before any integration call happens
    with pytest.raises(PermissionError):
        ToolRegistry.query_osint_signals("x", caller_agent="LEGAL_REGULATORY")
    assert ToolRegistry.get_audit_trail()[-1].status == "PERMISSION_DENIED"



def test_signals_relevance_ranking_and_limit(service):
    """Signals are ranked by relevance and respect the limit."""
    signals = service.get_signals(TOPIC, limit=5)
    assert 0 < len(signals) <= 5
    relevances = [s.relevance for s in signals]
    assert relevances == sorted(relevances, reverse=True)
    top = signals[0]
    assert top.source in {
        "Global Tech & Industry Wire", "Government Tender Board", "Industry Social Pulse",
        "Patent Filing Monitor", "Analyst Briefings"
    }
    assert 0.0 <= top.sentiment_score <= 1.0 or -1.0 <= top.sentiment_score < 0.0


def test_trends_momentum_and_spike_detection(service):
    """Trend detection aggregates tag volume with direction and spikes."""
    trends = service.get_trends(TOPIC)
    assert len(trends) >= 3
    topics_seen = {t.topic for t in trends}
    assert "sovereign-ai" in topics_seen
    for t in trends:
        assert t.volume >= 1
        assert -1.0 <= t.momentum <= 1.0
        assert 0.0 < t.confidence <= 1.0
        assert t.direction.value in {"UP", "DOWN", "STABLE"}
    top = trends[0]
    assert top.volume == max(x.volume for x in trends)


def test_api_key_authentication(monkeypatch):
    """When auth is enabled every route except /health requires a valid X-API-Key."""
    from business_market_intelligence import auth

    monkeypatch.setattr(auth, "configured_api_key", lambda: "s3cret")
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/intelligence/signals", params={"topic": "defense AI"}).status_code == 401
    assert client.get(
        "/intelligence/signals", params={"topic": "defense AI"},
        headers={"X-API-Key": "s3cret"}
    ).status_code == 200


def test_input_validation_limits():
    """Query parameters are bounded (rejects empty/oversized topics and limits)."""
    client = TestClient(app)
    assert client.get("/intelligence/signals", params={"topic": "x"}).status_code == 422
    assert client.get("/intelligence/signals", params={"topic": "a" * 900}).status_code == 422
    assert client.get(
        "/intelligence/signals", params={"topic": "defense AI", "limit": 0}
    ).status_code == 422



def test_external_osint_provider_seam():
    """Person A's Domain 2 plugs in via set_external_provider; failures fall back silently."""
    from business_market_intelligence.sources import set_external_provider
    from business_market_intelligence.models import SourceType

    class _FakeOSINT:
        def collect(self, topic, limit=10):
            return [{
                "title": f"Live crawl hit for {topic}",
                "body": "fresh external intelligence with strong growth",
                "published": "2026-09-24T12:00:00",
                "url": "https://osint.example.com/live",
                "tags": ["live-crawl"],
                "source_name": "Live OSINT (Domain 2)",
            }]

    try:
        set_external_provider(_FakeOSINT())
        items = collect_raw_items("defense AI")
        external = [i for i in items if i.source_type is SourceType.EXTERNAL]
        assert len(external) == 1
        assert external[0].source_name == "Live OSINT (Domain 2)"

        signals = MarketIntelligenceService().get_signals("defense AI", limit=20)
        assert any(s.source == "Live OSINT (Domain 2)" for s in signals)

        class _Broken:
            def collect(self, topic, limit=10):
                raise RuntimeError("crawler down")

        set_external_provider(_Broken())
        assert len(collect_raw_items("defense AI")) > 0   # static sources still work
    finally:
        set_external_provider(None)

