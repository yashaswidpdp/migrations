"""
Local schema definitions for Vendor, Vendor User, and Vendor Activity migration.

Odoo source: Partner/Vendor endpoints or res.partner (UAT)
Flask target:
  POST /api/vendor/create
  POST /api/vendor-activity/create
"""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class OdooVendor:
    id: int
    company_name: str
    vendor_id: str                      # Unique vendor identifier (e.g. VND-01)
    location: Optional[str] = None
    website: Optional[str] = None
    status: str = "Active"              # Active | Inactive
    contract_start: Optional[str] = None # Date string
    contract_end: Optional[str] = None   # Date string
    response_sla: Optional[int] = None   # SLA in days
    risk_level: str = "Low"             # Low | Medium | High | Critical
    vra_status: str = "Pending"         # Completed | In Progress | Pending
    processing_activities: List[dict] = field(default_factory=list)


@dataclass
class OdooVendorUser:
    id: int
    name: str
    email: str
    phone: str
    active: bool = True
    vendor_id: Optional[int] = None     # References parent OdooVendor id


@dataclass
class OdooVendorActivity:
    id: int
    vendor_id: int
    user_id: int                        # Assigned vendor contact (User)
    request_id: Optional[int] = None
    consent_id: Optional[int] = None
    state: str = "initiated"            # initiated | in_progress | completed
    request_date: Optional[str] = None
    completed_date: Optional[str] = None
    comment: Optional[str] = None
    attachment: Optional[str] = None


@dataclass
class FlaskCreateVendorPayload:
    company_name: str
    user_id: int                        # DPO/PAManager User ID on backend
    vendor_id: Optional[str] = None     # Generates if empty
    location: Optional[str] = None
    website: Optional[str] = None
    status: str = "Active"
    contract_start: Optional[str] = None
    contract_end: Optional[str] = None
    response_sla: Optional[int] = None
    vra_status: str = "Pending"
    risk_level: str = "Low"
    processing_activity_ids: List[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "company_name": self.company_name,
            "user_id": self.user_id,
            "status": self.status,
            "vra_status": self.vra_status,
            "risk_level": self.risk_level,
        }
        if self.vendor_id:
            d["vendor_id"] = self.vendor_id
        if self.location:
            d["location"] = self.location
        if self.website:
            d["website"] = self.website
        if self.contract_start:
            d["contract_start"] = self.contract_start
        if self.contract_end:
            d["contract_end"] = self.contract_end
        if self.response_sla is not None:
            d["response_sla"] = self.response_sla
        if self.processing_activity_ids:
            d["processing_activity_ids"] = self.processing_activity_ids
        return d


@dataclass
class FlaskCreateVendorActivityPayload:
    user_id: int                        # Vendor user ID
    vendor_id: int                      # Vendor ID
    request_id: Optional[int] = None
    consent_id: Optional[int] = None
    request_date: Optional[str] = None
    completed_date: Optional[str] = None
    state: str = "initiated"
    comment: Optional[str] = None
    attachment: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "user_id": self.user_id,
            "vendor_id": self.vendor_id,
            "state": self.state,
        }
        if self.request_id is not None:
            d["request_id"] = self.request_id
        if self.consent_id is not None:
            d["consent_id"] = self.consent_id
        if self.request_date:
            d["request_date"] = self.request_date
        if self.completed_date:
            d["completed_date"] = self.completed_date
        if self.comment:
            d["comment"] = self.comment
        if self.attachment:
            d["attachment"] = self.attachment
        return d
