https://tech.portal-uat.dpdpconsultants.com/


/api/dpgr/dashboard
{
  "processing_activity": 3,
  "department_name": "HR",
  "state": "open",
  "sla_key": "red",
  "email": "test@gmail.com",
  "page_no": 1,
  "rec_limit": 10
}
 
/api/dpcm/dashboard
{
  "state": "Consented",
  "page_no": 1,
  "rec_limit": 10,
  "email": "test@gmail.com",
  "department_name": "HR",
  "processing_activity": 3,
  "is_vendor_email": false,
  "page_no": 1,
  "rec_limit": 50
}


● Checked transforms + models. State:

  Consent — left

  Transform emits: name, email, phone, PA id+name, manager_name, status, 
  legacyType, consentType, processingType, consent_date, sent_on, delivered_on, 
  valid_till, consent_reject_on, accept_terms.

  Gaps:
  1. consent_lifecycle — not derived anywhere. Your enum (Legacy 
  Consent/Consented/Rejected/Not Delivered/Withdrawn/Expired/Bounced) has no
  producer. This is the field from last question. Add derive in
  transform_consent.py (order: legacy→expired→not-delivered→status).
  [[consent-type-mapping]]
  2. Digital date loss — /consent/import LEGACY mode has no date column →
  consent_date dropped, created_at=now. Backend limit.
  load_consents_via_migration (/migration/consent) fixes — preserves all dates.
  Pick that path. [[migration-ext-pattern]]
  3. consentRejectOn misnamed — filled even when status=Consented. Don't trust
  for REJECTED until Odoo source confirms meaning.
  4. pAManager — dropped per your call. OK.

  Request — left

  Transform emits: name, email, phone, request_type_id, PA names, 
  assigned_users, status, rag_status, otp_required, raised_on, action_date, 
  resolution_date, closed_on.

  Gaps:
  1. request_type_id hardcoded =1 — DPGR type not in dashboard. Loader tries
  /request-types/ first record as fallback. Still blocked on Odoo source.
  [[request-type-gap]]
  2. requestNo → request_no NOT emitted — Odoo model has requestNo, transform
  skips it. Add it.
  3. risk — FLASK_RISK_LEVELS defined, never mapped. No Odoo source field. Gap
  or N/A.
  4. days_since_raised — Odoo DaysSinceRequestIsRaised in model, transform
  drops. Add if backend wants.
  5. assigned_users — empty unless assigned_user_id passed at transform call.
  Confirm caller passes it.
  
  Biggest blockers

  - Consent: wire consent_lifecycle derive (actionable now).
  - Request: request_type_id DPGR gap (needs Odoo source) + request_no missing
  (actionable now).

  Want me add consent_lifecycle derive to transform_consent.py + request_no to
  transform_request.py?

