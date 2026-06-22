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
    requestNo: str = ""
    actionDate: Optional[str] = None
    DaysSinceRequestIsRaised: Optional[int] = None
    createOn: Optional[str] = None
    resolutionDate: Optional[str] = None
    closedOn: Optional[str] = None
    # by-id (/dpgr/id) extra fields. dpComment/escalatedComment/escalatedDate/
    # closingComment/withdrawalComment are free-text + close/escalation; iPAddress/
    # deviceType are DPDP request-proof. consent -> revoke-link; trackAssigneeStatus
    # -> real allottee identity/state; assignToDM -> internal allottee;
    # assignToVendor -> vendor<->request activity link (all now migrated).
    # attachment/dataDiscovery still have no create-path target -> dropped.
    dpComment: Optional[str] = None
    escalatedComment: Optional[str] = None
    escalatedDate: Optional[str] = None
    closingComment: Optional[str] = None
    withdrawalComment: Optional[str] = None
    iPAddress: Optional[str] = None
    deviceType: Optional[str] = None
    # Vendor handling the request + internal allottee (Data Manager); each a
    # `[ {id, name} ]` array on /dpgr/id.
    assignToVendor: List[dict] = field(default_factory=list)
    assignToDM: List[dict] = field(default_factory=list)
    trackAssigneeStatus: List[dict] = field(default_factory=list)


@dataclass
class FlaskRequestPayload:
    odoo_source_id: int
    name: str
    email: str
    phone: str
    request_type: int
    processing_activity_names: List[str] = field(default_factory=list)
    # Odoo consent ids to withdraw (revoke requests); backend resolves via source-map.
    consent_source_ids: List[int] = field(default_factory=list)
    # Vendor<->request "activity" link from /dpgr/id `assignToVendor`. Odoo
    # vendor-contact ids + names; backend resolves to Flask vendors via the
    # vendor source-map (name is the resolution fallback).
    assigned_vendor_source_ids: List[int] = field(default_factory=list)
    assigned_vendor_names: List[str] = field(default_factory=list)
    # Real internal allottee from /dpgr/id `assignToDM`; loader resolves
    # NAMES -> Flask user ids (assigned_users) via the backend-user catalogue.
    assigned_user_names: List[str] = field(default_factory=list)
    # Allotment state + when, from trackAssigneeStatus (e.g. "assigned_dm").
    assignee_status: Optional[str] = None
    assignee_raised_on: Optional[str] = None
    status: str = "Initiated"
    rag_status: str = "Green"
    otp_required: bool = False
    request_no: Optional[str] = None
    action_date: Optional[str] = None
    days_since_raised: Optional[int] = None
    created_on: Optional[str] = None
    resolution_date: Optional[str] = None
    closed_on: Optional[str] = None
    # Flask target columns: dp_comment / escalated_comment / escalated_date /
    # closed_comment / ip_address / device_type. is_escalated is derived backend-side.
    dp_comment: Optional[str] = None
    escalated_comment: Optional[str] = None
    escalated_date: Optional[str] = None
    closed_comment: Optional[str] = None
    ip_address: Optional[str] = None
    device_type: Optional[str] = None

