"""
Domain 9 REST Gateway — Legal, Regulatory & Intellectual Property Intelligence API.
Run: uvicorn legal_regulatory_ip_intelligence.api:app --port 8003
"""
from typing import Annotated, List, Optional
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from .config import load_config
from .service import LegalIntelligenceService
from .auth import require_api_key
from .models import ComplianceAnalysis, IntelligenceReport, IPAnalysis, LegalRisk

_api_cfg = load_config().get("api", {})

app = FastAPI(
    title=_api_cfg.get("title", "Vexer Domain 9 — Legal, Regulatory & IP Intelligence API"),
    version=_api_cfg.get("version", "1.0.0"),
    description=_api_cfg.get("description", "Regulatory obligations, IP clearance and legal risk intelligence.")
)

service = LegalIntelligenceService()
_AUTH = [Depends(require_api_key)]
TopicText = Annotated[str, Field(max_length=200)]


class ScopeRequest(BaseModel):
    scope: str = Field(..., min_length=2, max_length=2000)
    topics: Optional[List[TopicText]] = Field(default=None, max_length=20)


class ReportRequest(BaseModel):
    subject: str = Field(..., min_length=2, max_length=2000)
    topics: Optional[List[TopicText]] = Field(default=None, max_length=20)


class IPReviewRequest(BaseModel):
    topics: List[TopicText] = Field(default_factory=list, max_length=20)
    subject_text: str = Field(default="", max_length=2000)


@app.get("/health")
def health_check():
    return service.health()


@app.get("/regulations", dependencies=_AUTH)
def get_regulations():
    """Regulations catalog with triggers, obligations and penalty notes."""
    return service.get_regulations()


@app.get("/ip-landscape", dependencies=_AUTH)
def get_ip_landscape():
    """Known patents and trademarks with status, owner and expiry."""
    return service.get_ip_landscape()


@app.post("/compliance/analyze", response_model=ComplianceAnalysis, dependencies=_AUTH)
def analyze_compliance(req: ScopeRequest):
    """Detects applicable regulations, obligations, flags and sign-off policy."""
    return service.analyze(req.scope)


@app.post("/ip/clearance", response_model=IPAnalysis, dependencies=_AUTH)
def clear_ip(req: IPReviewRequest):
    """IP freedom-to-operate screening for given product topics."""
    return service.clear_ip(req.topics or None, req.subject_text)


@app.post("/legal/risk", response_model=LegalRisk, dependencies=_AUTH)
def legal_risk(req: ScopeRequest):
    """Blended legal risk score and level (compliance + IP exposure)."""
    return service.legal_risk(req.scope, req.topics)


@app.post("/report", response_model=IntelligenceReport, dependencies=_AUTH)
def build_report(req: ReportRequest):
    """Full legal, regulatory and IP intelligence report."""
    return service.build_report(req.subject, req.topics)
