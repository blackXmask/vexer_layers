"""
Domain 7 REST Gateway — Business & Market Intelligence API.
Run: uvicorn business_market_intelligence.api:app --port 8001
"""
from typing import List, Optional
from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field

from .config import load_config
from .service import MarketIntelligenceService
from .auth import require_api_key
from .models import CompetitorProfile, IntelligenceReport, MarketSegment, MarketSignal, Trend

_api_cfg = load_config().get("api", {})

app = FastAPI(
    title=_api_cfg.get("title", "Vexer Domain 7 — Business & Market Intelligence API"),
    version=_api_cfg.get("version", "1.0.0"),
    description=_api_cfg.get("description", "Signals, trends, competitors and reports.")
)

service = MarketIntelligenceService()

_TOPIC = Query(..., min_length=2, max_length=500, description="Topic to analyse (2-500 chars)")
_AUTH = [Depends(require_api_key)]


class ReportRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=500)
    max_signals: Optional[int] = Field(default=None, ge=1, le=100)


@app.get("/health")
def health_check():
    return service.health()


@app.get("/intelligence/signals", response_model=List[MarketSignal], dependencies=_AUTH)
def get_signals(topic: str = _TOPIC, limit: Optional[int] = Query(default=None, ge=1, le=100)):
    """Scored market signals with sentiment, relevance and provenance."""
    return service.get_signals(topic, limit)


@app.get("/intelligence/trends", response_model=List[Trend], dependencies=_AUTH)
def get_trends(topic: str = _TOPIC):
    """Tag-level momentum, direction and spike detection."""
    return service.get_trends(topic)


@app.get("/intelligence/competitors", response_model=List[CompetitorProfile], dependencies=_AUTH)
def get_competitors(topic: str = _TOPIC):
    """Competitor threat profiles blended with observed mentions."""
    return service.get_competitors(topic)


@app.get("/intelligence/segments", response_model=List[MarketSegment], dependencies=_AUTH)
def get_segments():
    """Market segments with TAM/SAM and attractiveness scoring."""
    return service.get_segments()


@app.post("/intelligence/report", response_model=IntelligenceReport, dependencies=_AUTH)
def build_report(req: ReportRequest):
    """Full executive intelligence report for a topic."""
    report = service.build_report(req.topic)
    if req.max_signals is not None:
        report.signals = report.signals[:max(req.max_signals, 0)]
    return report
