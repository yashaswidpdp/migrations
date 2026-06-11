"""
Local schema definitions for Consent migration.

Odoo source: GET /api/dpcm/dashboard
Flask target:
  POST /api/consent/import          (deemed/legacy consents — Excel upload)
  POST /api/consent/live-consent    (live/active consents — JSON)
"""
from dataclasses import dataclass
from typing import Optional


FLASK_CONSENT_STATUSES = {
    "Initiated",
    "Deemed Consent",
    "Consented",
    "Rejected",
    "Not Delivered",
    "Withdrawn",
    "Expired",
    "Bounced",
    "Delivered",
}

FLASK_LEGACY_TYPES = {"Legacy", "Live"}

FLASK_CONSENT_TYPES = {"Digital", "Paper"}

FLASK_PROCESSING_TYPES = {"Mandatory/Regulatory", "Promotional"}


@dataclass
class OdooConsent:
    id: int
    name: object          
    eMail: str
    phone: str
    processingActivity: object  
    status: str
    userActivityType: str
    paperType: str
    legacyType: str
    pAManager: object 

@dataclass
class FlaskConsentPayload:
    odoo_source_id: int
    name: str
    email: str
    phone: str
    processing_activity_name: Optional[str]   
    status: str
    processingType: str    
    consentType: str       
    legacyType: str        
    accept_terms: bool = True
    otp_required: bool = False
