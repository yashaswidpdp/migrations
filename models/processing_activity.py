"""
Local schema definitions for Processing Activity migration.

Odoo source: GET /api/processing_activities
Flask target: POST /api/processing/create
"""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class OdooProcessingActivity:
    id: int
    name: str
    level: int
    isOtpMandatory: bool = False
    processingActivityType: str = "Mandatory/Regulatory"
    isActive: bool = True
    showOnDpgr: bool = False
    showOnDpia: bool = False
    description: str = ""
    managerId: Optional[object] = None
    otpExpiryConsent: str = ""
    consentEmailTemplateId: Optional[object] = None
    consentEmailEffectiveFrom: str = ""
    consentSmsTemplateId: Optional[object] = None
    consentSmsEffectiveFrom: str = ""
    privacyTemplateId: Optional[object] = None
    privacyEffectiveFrom: str = ""
    consentValidity: str = ""
    children: List["OdooProcessingActivity"] = field(default_factory=list)



FLASK_PA_ACTIVITY_TYPES = {
    "Mandatory/Regulatory",
    "Promotional",
}


@dataclass
class FlaskCreatePAPayload:
    name: str
    manager_ids: List[int] = field(default_factory=list)
    description: Optional[str] = None
    parent_id: Optional[int] = None          
    activity_type: str = "Mandatory/Regulatory"
    is_active: bool = True
    is_otp: bool = False
    show_on_dpgr: bool = False
    show_on_privacy: bool = False
    consent_validity_months: Optional[int] = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "manager_ids": self.manager_ids,
            "activity_type": self.activity_type,
            "is_active": self.is_active,
            "is_otp": self.is_otp,
            "show_on_dpgr": self.show_on_dpgr,
            "show_on_privacy": self.show_on_privacy,
        }
        if self.description:
            d["description"] = self.description
        if self.parent_id is not None:
            d["parent_id"] = self.parent_id
        if self.consent_validity_months is not None:
            d["consent_validity_months"] = self.consent_validity_months
        return d
