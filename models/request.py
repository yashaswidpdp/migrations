"""
Local schema definitions for Request (DPGR) migration.

Odoo source: GET /api/dpgr/dashboard
Flask target: POST /api/request/create
"""
from dataclasses import dataclass, field
from typing import Optional, List


FLASK_REQUEST_STATUSES = {
    "Completed",
    "Initiated",
    "Assign to PA Manager",
    "Assign to DPO",
}

FLASK_RAG_STATUSES = {"Red", "Amber", "Green", "Completed"}

FLASK_RISK_LEVELS = {"High", "Medium", "Low"}


@dataclass
class OdooRequest:
    id: int
    name: str
    eMail: str
    phone: str
    processingActivity: List[dict]
    pAManager: object 
    status: str
    ragStatus: str


@dataclass
class FlaskRequestPayload:
    odoo_source_id: int
    name: str
    email: str
    phone: str
    request_type_id: int             
    processing_activity_names: List[str] = field(default_factory=list)
    assigned_user_names: List[str] = field(default_factory=list)
    status: str = "Initiated"
    rag_status: str = "Green"
    otp_required: bool = False
