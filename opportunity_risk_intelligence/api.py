"""
Domain 8 REST Gateway — Opportunity, Risk & Requirements Intelligence API.
Run: uvicorn opportunity_risk_intelligence.api:app --port 8002
"""
from typing import Annotated, List
from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from .config import load_config
from .service import OpportunityRiskService
from .auth import require_api_key
from .models import (
    IntelligenceReport,
    OpportunityAssessment,
    RequirementItem,
    RiskAnalysis,
)

_api_cfg = load_config().get("api", {})

app = FastAPI(
    title=_api_cfg.get("title", "Vexer Domain 8 — Opportunity, Risk & Requirements Intelligence API"),
    version=_api_cfg.get("version", "1.0.0"),
    description=_api_cfg.get("description", "RFP capability fitting, risk matrices and requirement intelligence.")
)

service = OpportunityRiskService()
_AUTH = [Depends(require_api_key)]
RequirementText = Annotated[str, Field(max_length=500)]


class RequirementsRequest(BaseModel):
    rfp_title: str = Field(default="", max_length=500)
    requirements: List[RequirementText] = Field(default_factory=list, max_length=100)


class ParseRequest(BaseModel):
    requirements: List[RequirementText] = Field(default_factory=list, max_length=100)


@app.get("/health")
def health_check():
    return service.health()


@app.get("/capabilities", dependencies=_AUTH)
def get_capabilities():
    """Configured capability catalog (name, category, weight, keywords)."""
    return service.get_capabilities()


@app.post("/requirements/parse", response_model=List[RequirementItem], dependencies=_AUTH)
def parse_requirements(req: ParseRequest):
    """Classifies requirements into category + priority and matches capabilities."""
    return service.parse_requirements(req.requirements)


@app.post("/opportunity/assess", response_model=OpportunityAssessment, dependencies=_AUTH)
def assess_opportunity(req: RequirementsRequest):
    """Fit score, GO/NO-GO/CONDITIONAL recommendation, category breakdown."""
    return service.assess(req.rfp_title, req.requirements)


@app.post("/risks/analyze", response_model=RiskAnalysis, dependencies=_AUTH)
def analyze_risks(req: RequirementsRequest):
    """Triggered risk register entries with severity bands and mitigations."""
    return service.analyze_risks(req.rfp_title, req.requirements)


@app.post("/report", response_model=IntelligenceReport, dependencies=_AUTH)
def build_report(req: RequirementsRequest):
    """Full opportunity + risk intelligence report for an RFP."""
    return service.build_report(req.rfp_title, req.requirements)
