# Migration Alignments & Backend Synchronization Documentation

This document provides a detailed overview of the alignment changes, data transformations, and model synchronizations implemented in the `migration/` codebase to match the latest specifications, endpoints, and validation requirements of the `dpdp_python` Flask backend.

---

## 1. Notice Template Sync & Multi-PA Support

Several updates were made to synchronize notice template definitions and support the updated structure of the backend APIs.

### Schema Alignment
* **File**: [template.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/models/template.py)
* **Changes**:
  * Added the `enable_granular_consent` property (`bool`) to the `FlaskCreateTemplatePayload` dataclass.
  * Replaced the singular `processing_activity_id` field with `processing_activity_ids` (`List[int]`) in `FlaskCreateTemplatePayload` to support multi-processing activity mappings.

### Extraction & Transformations
* **File**: [transform_template.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/scripts/transform/transform_template.py)
* **Changes**:
  * **Strict Enum Values**: Aligned `_map_template_type` to map legacy Odoo template types to the exact backend enum values expected by the Flask validators (e.g. `"Legacy Consent Email Template"`, `"Privacy Notice Email Template"`, `"Live Consent Template"`).
  * **Language Normalization**: Updated the `FLASK_LANGUAGES` map to correctly translate to `"Santhali"` (matching the database constraints) instead of the legacy spelling `"Santali"`.
  * **Granular Flag Extract**: Mapped the `"is_granular_consent"` configuration flag from the Odoo raw data to `"is_granular"` in the output CSV.
  * **Multi-PA Resolution**: Modified extract logic to save all processing activity names associated with a template as a list representation under `"processing_activity_names"` rather than a single string.

### Loader Updates
* **File**: [load_flask.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/scripts/load/load_flask.py)
* **Changes**:
  * **Endpoint Routing**: Updated notice templates endpoint prefixes from `/templates/` and `/templates/create` to `/notice-templates/` and `/notice-templates/create`.
  * **Resolution Parsing**: Rewrote `_resolve_pa_ids()` to fetch from the active simple activities endpoint (`/api/processing/activities/simple`) and correctly parse the nested `data.records` structures.
  * **Granular Mapping**: Linked the CSV `"is_granular"` column to the `"enable_granular_consent"` field in the POST payload.
  * **Multi-PA Mapping**: Parsed the list representation from `"processing_activity_names"`, mapped each name to its respective ID using `_resolve_pa_ids()`, and sent them under `"processing_activity_ids"` as a list of integers.

---

## 2. Processing Activity (PA) & Manager Validation Alignment

The Flask route `/api/processing/create` enforces a strict validation block requiring a non-empty `manager_ids` list containing valid backend user IDs.

### Schema Alignment
* **File**: [processing_activity.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/models/processing_activity.py)
* **Changes**:
  * Added the `manager_ids` property (`List[int]`) to the `FlaskCreatePAPayload` dataclass.

### Transformations
* **File**: [transform_processing_activity.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/scripts/transform/transform_processing_activity.py)
* **Changes**:
  * Added logic during tree flattening to inspect the `managerId` field from raw Odoo JSON (handling lists like `[id, name]`, objects, or plain strings) and extract it as a string to the new `"manager_name"` column of the flat CSV.

### Loader Updates
* **File**: [load_flask.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/scripts/load/load_flask.py)
* **Changes**:
  * **User Name Mapping**: Added `_resolve_user_ids()` to retrieve all active DPO and PAManager users from `/api/auth/backend-users` and map their names to user IDs.
  * **DPO Fallback**: Added `_resolve_current_user_id()` to query `/api/auth/profile` and fetch the profile ID of the currently logged-in user running the migration.
  * **Manager Mapping**: Updated `load_processing_activities()` to look up the `"manager_name"` of each record in the resolved users map. If not found or empty, it defaults to the active authenticated user ID, and sends the resolved ID inside the `"manager_ids"` array in the POST payload.

---

## 3. Deemed Consent Formatting Alignment

The Flask endpoint `/api/consent/import` expects form data keys to match specific camelCase variables.

### Loader Updates
* **File**: [load_flask.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/scripts/load/load_flask.py)
* **Changes**:
  * Renamed keys sent in the POST request to `/consent/import` from snake_case (`legacy_type`, `consent_type`, `processing_type`) to camelCase (`legacyType`, `consentType`, `processingType`).

---

## 4. Odoo Schema Verification & Vendor Alignments

To ensure the local models represent a complete and robust replica of Odoo's entities, we verified all fields (excluding system-imposed unique IDs) and expanded definitions.

### Consent Model Extension
* **File**: [consent.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/models/consent.py)
* **Changes**:
  * Added `consentRejectOn`, `validTill`, `sentOn`, and `deliveredOn` fields to `OdooConsent`.
  * Added matching `consent_reject_on`, `valid_till`, `sent_on`, and `delivered_on` properties to `FlaskConsentPayload`.

### Request Model Extension
* **File**: [request.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/models/request.py)
* **Changes**:
  * Added `requestNo`, `actionDate`, `DaysSinceRequestIsRaised`, `createOn`, `resolutionDate`, and `closedOn` fields to `OdooRequest`.
  * Added matching payload fields `request_no`, `action_date`, `days_since_raised`, `created_on`, `resolution_date`, and `closed_on` to `FlaskRequestPayload`.

### Vendor, Vendor User & Vendor Activity Models
* **File**: [vendor.py](file:///home/yashaswi/Developer/migrations_odoo_flask/migration/models/vendor.py) [NEW]
* **Changes**:
  * Created Python dataclasses `OdooVendor`, `OdooVendorUser`, and `OdooVendorActivity` representing all fields and relationships in the source Odoo CRM/res.partner database.
  * Added corresponding JSON REST payload builders `FlaskCreateVendorPayload` and `FlaskCreateVendorActivityPayload` to enable complete, verified schema loading.

---

## 5. Environment & Repository Configurations

* **File**: [.gitignore](file:///home/yashaswi/Developer/migrations_odoo_flask/.gitignore)
* **Changes**:
  * Created a workspace root `.gitignore` ignoring Python virtual environments, database files, credential configuration files, log files, raw/processed migration data, and `.claude` configuration folders.

