---
name: attachment-storage-convention
description: How attachments are stored in the dpdp_python Flask backend (no table; path-string columns)
metadata:
  type: project
---

dpdp_python backend has **no Attachment model/table**. Attachments are relative-path string columns on each resource, file bytes on local disk under `uploads/<resource>/<uuid>_<secure_name>`.

Columns: `Request.attachment` / `escalated_attachment` / `closed_attachment`, `RequestAssignmentTrack.attachment` (→ `uploads/requests`); `Vendor.nda_document`/`contract_document`/`sow_document`/`dpa_document`/`other_documents` (→ `uploads/vendors`); `VendorActivity.attachment` (→ `uploads/vendor_activities`).

Upload service to REUSE: `utils/file_upload.py:upload_file(file, upload_folder, allowed_ext, max_size)` — wants a Werkzeug `FileStorage`-like obj (`.filename/.seek/.tell/.save`), validates ext+10MB, returns relative path. URL via `build_file_url(path)`. Download + per-role authz in `routes/uploads.py` (matches path columns by suffix).

**IMPLEMENTED (2026-06-17):** backend pkg `dpdp_python/migration_ext/attachments/` (constants/validators/decoder/mapper/uploader + `process_vendor_attachments`). `migrate_vendor` route decodes payload `attachments` and sets columns. ETL: `transform_vendor._extract_attachments` writes decoded bytes to `data/attachments/vendor/<id>/` + `data/processed/vendor_attachments_manifest.json`; loader `_load_vendor_attachment_manifest`/`_build_attachments` re-encodes into payload. Mapping: nda_attachment→nda_document, vra_attachment→**dpa_document** (user chose dedicated slot). Verified byte-exact end-to-end vs real sample. REMAINING GAP: extract `/vendors_details` list endpoint lacks fileContent — need a vendor by-id enrichment (endpoint unconfirmed; sample came from `web/content/vendor.dashboard/<id>/...`) to populate the attachment objects before transform.

**Attachment migration design decisions (2026-06-17):** storage paths must match `uploads/requests` / `uploads/vendors` (NOT the doc's `request/`/`vendor/`, which breaks download routes); attachments flow via sidecar files + JSON manifest (no base64 in CSV); centralize decode/upload in `dpdp_python/migration_ext/attachments/`. Encoding RESOLVED (vendor sample 2026-06-17): by-id response carries `vra_attachment`/`nda_attachment` = `{fileName, fileContent, downloadUrl}`. `fileContent` is STANDARD BASE64 ASCII (strict decode OK, no data: prefix) → `base64.b64decode` → real bytes (verified PNG via magic). `fileName` has real ext; `downloadUrl` is old Odoo link, IGNORED. Decoder: b64decode → FileStorage(BytesIO) → `upload_file`. Mime from magic bytes, fallback `mimetypes.guess_type(fileName)`. Map nda_attachment→nda_document, vra_attachment→other_documents. See [[migration-pipeline]].
