# Endpoint and Model Mapping Table

This document outlines the source-to-destination API mappings derived from the legacy Odoo system and the new Flask application.

> **Enum values verified against**: `dpdp_python/models/consent.py` and `dpdp_python/models/request.py` on 2026-04-27.

## 1. Data Protection Grievances / Requests (DPGR)
Used to migrate Data Subject Access Requests (DSAR) and Grievances.

| System | API Endpoint | Method | Purpose |
|--------|--------------|--------|---------|
| **Source (Odoo)** | `/dpgr/dashboard` | POST | Fetch list of requests with offset pagination (`page_no`, `rec_limit`) |
| **Destination (Flask)**| `/request/create` | POST | Create individual data subject request |

**Transformation Map (Odoo `dpgrData` -> Flask `Request`):**
- `id` -> `odoo_source_id` (Keep for logging/idempotency)
- `name` -> `name` (string, direct)
- `eMail` -> `email`
- `phone` -> `phone`
- `status` -> `status`:
  - `"Not Assigned"` -> `"Initiated"`
  - `"Assign to PA Manager"` -> `"Assign to PA Manager"`
  - `"Assigned To DPO"` -> `"Assign to DPO"`
  - `"Completed"` -> `"Completed"`
- `ragStatus` -> (not sent to API, but validated as `Red` | `Amber` | `Green` | `Completed`)
- `processingActivity` (list of dicts `[{"id": 26, "name": "..."}]`) -> `processing_activity` (list of integers `[26]`)
- `pAManager` (list `[14, "Rahul"]`) -> `assigned_users` (list of integers `[14]`)
- `request_type_id` -> Defaulted to `1` (Missing from source JSON)
- `otp_required` -> `false` (Skip OTP for migration)

---

## 2. Data Protection Consent Management (DPCM)
Used to migrate User Consents.

| System | API Endpoint | Method | Purpose |
|--------|--------------|--------|---------|
| **Source (Odoo)** | `/dpcm/dashboard` | POST | Fetch list of user consents with offset pagination |
| **Destination (Flask)**| `/consent/live-consent` | POST | Create individual consent record |

**Transformation Map (Odoo `dpcmData` -> Flask `Consent`):**
- `id` -> `odoo_source_id` (Keep for logging/idempotency tracking)
- `name` (array `[id, "Name"]`) -> `name` (string, index 1)
- `eMail` -> `email`
- `phone` -> `phone`
- `status` -> `status`:
  - `"Deemed consent"` -> `"Deemed Consent"`
  - `"Consented"` -> `"Consented"`
  - `"Rejected"` -> `"Rejected"`
  - `"Withdrawn"` -> `"Withdrawn"`
- `processingActivity` (array `[id, "Name"]`) -> `processing_activity_id` (integer, index 0)
- `legacyType` -> `legacyType`:
  - `"legacy"` -> `"Legacy"`
  - `"live"` -> `"Live"`
- `paperType` -> `consentType`:
  - `"digital"` -> `"Digital"`
  - `"paper"` -> `"Paper"`
  - ~~`"verbal"` -> removed, defaults to `"Digital"`~~
- `userActivityType` -> `processingType`:
  - `"mandatory"` -> `"Mandatory/Regulatory"`
  - `"promotional"` -> `"Promotional"`
- `otp_required` -> `false` (Skip OTP for migration)
- `accept_terms` -> `true` (Hardcoded)

---

## ⚠️ Architecture Notes

The Flask destination routes (`/consent/live-consent`, `/request/create`) were designed for live user interactions. They:
- Hardcode some enum values (e.g., `legacyType=LIVE` in `/live-consent`)
- Trigger email sending
- Consume licenses
- Require tenant context from the Host header

For production migration, consider creating dedicated `/migrate` endpoints that skip side effects.
