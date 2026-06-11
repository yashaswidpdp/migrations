"""
Local schema definitions for Template migration.

Odoo source: GET /api/v2/get/templates
Flask target: POST /api/templates/create
"""
from dataclasses import dataclass, field
from typing import Optional, List


ODOO_TO_FLASK_TEMPLATE_TYPE = {
    "consent": "Consent",
    "privacy": "Privacy Notice",
    "online": "Live Consent",
    "email": "Email Template",
    "sms": "SMS Template",
    "breach": "Breach Notice",
    "notification": "Notification",
}


FLASK_TEMPLATE_TYPES = {
    "Breach Notice Email Template",
    "Breach Notice SMS Template",
    "Privacy Notice Email Template",
    "Privacy Notice SMS Template",
    "Legacy Consent Email Template",
    "Consent SMS Template",
    "Live Consent Template",
    "SMS OTP Template",
    "Nominee Templates",
}


ODOO_TO_FLASK_SUB_TYPE = {
    "email": "Email",
    "sms": "SMS",
    "online": "Online",
    "msg91": "MSG91",
    
}

FLASK_SUB_TYPES = {"SMS", "Email", "Online", "MSG91"}

FLASK_LANGUAGES = {
    "Assamese", "Bengali", "Bodo", "Dogri", "English", "Gujarati",
    "Hindi", "Kannada", "Kashmiri", "Konkani", "Maithili", "Malayalam",
    "Manipuri", "Marathi", "Nepali", "Odia", "Punjabi", "Sanskrit",
    "Santhali", "Sindhi", "Tamil", "Telugu", "Urdu",
}

FLASK_STATUS_VALUES = {"Active", "Draft", "Archive"}

@dataclass
class OdooTemplateLanguage:
    id: int
    name: str


@dataclass
class OdooTemplatePA:
    id: int
    name: str


@dataclass
class OdooTemplate:
    id: int
    name: str
    templateType: str
    subType: str
    templateBody: str
    language: Optional[OdooTemplateLanguage] = None
    processingActivities: List[OdooTemplatePA] = field(default_factory=list)
    sub_department: List[dict] = field(default_factory=list)
    isDefault: bool = False
    state: str = "accept"
    effectiveDate: Optional[str] = None
    templateSmsBody: str = ""
    is_granular_consent: bool = False


@dataclass
class FlaskCreateTemplatePayload:
    name: str
    template_type: str       
    sub_type: str            
    language: str
    email_body: str
    subject: Optional[str] = None
    is_default: bool = False
    approval: bool = False
    status: str = "Active"
    enable_granular_consent: bool = False
    processing_activity_ids: List[int] = field(default_factory=list)
    effective_from: Optional[str] = None

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "template_type": self.template_type,
            "sub_type": self.sub_type,
            "language": self.language,
            "email_body": self.email_body,
            "is_default": self.is_default,
            "enable_granular_consent": self.enable_granular_consent,
            "approval": self.approval,
            "status": self.status,
        }
        if self.subject:
            d["subject"] = self.subject
        if self.processing_activity_ids:
            d["processing_activity_ids"] = self.processing_activity_ids
        if self.effective_from:
            d["effective_from"] = self.effective_from
        return d
