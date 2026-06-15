"""
Local schema definitions for Consent migration.

Odoo source: GET /api/dpcm/dashboard
Flask target (split by consentType, both via the same Excel-upload endpoint):
  POST /api/consent/import  consentType=PAPER    (paperType=Paper)
      -> keeps consent_date (created_at/consented_on) and the real status,
         stores consentType=Paper.
  POST /api/consent/import  consentType=LEGACY   (paperType=Digital)
      -> backend forces status="Deemed Consent" and legacyType="Legacy";
         "LEGACY" is not a ConsentTypeEnum name so the stored consentType
         defaults to Digital. NOTE: the legacy importer has no date column,
         so the original consent_date is NOT preserved for digital records.

Both paths send a notice email per record (backend behaviour; cannot be
suppressed without changing the Flask backend).
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
    consentRejectOn: Optional[str] = None
    validTill: Optional[str] = None
    sentOn: Optional[str] = None
    deliveredOn: Optional[str] = None

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
    # Normalised dd/mm/YYYY consent date (from Odoo sentOn / deliveredOn).
    # Only honoured by the PAPER import path; ignored by the LEGACY path.
    consent_date: Optional[str] = None
    consent_reject_on: Optional[str] = None
    valid_till: Optional[str] = None
    sent_on: Optional[str] = None
    delivered_on: Optional[str] = None

