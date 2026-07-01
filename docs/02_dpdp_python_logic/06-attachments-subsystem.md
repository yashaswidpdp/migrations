# 06 — Attachments subsystem (`migration_ext/attachments/`)

Vendor migration carries NDA/VRA documents from Odoo as **inline Base64** in the
POST body. This package decodes, validates, and stores them through the *real*
upload service so a migrated document is indistinguishable from a UI-uploaded one.
The route layer contains **no** decoding logic — it calls one orchestrator.

## Payload contract
```json
"attachments": {
  "nda_attachment": {"fileName": "nda.png", "fileContent": "<base64>"},
  "vra_attachment": {"fileName": "vra.pdf", "fileContent": "<base64>"}
}
```

## Orchestrator — `process_vendor_attachments(payload, resource_id, state_at_upload)`
(`attachments/__init__.py`)

For each entry in `payload["attachments"]`:
1. `vendor_target(field)` — map the Odoo field to a `(vendor_column, upload_folder)`.
   Unknown fields are skipped silently.
2. `validate_entry(field, entry)` — shape/required-key check.
3. `decode_attachment(field, entry)` — Base64-decode + mime-sniff into a
   `FileStorage`.
4. `validate_ext(field, filename)` — allowed-extension check.
5. `store(file_storage, folder, resource_type="vendor", resource_id=…,
   document_type=…, state_at_upload=…)` — persist via the real `upload_file`
   service, producing the standardized filename
   `vendor_<id>_<doc>_<state>_<ts>_<uuid>.<ext>`.

Returns `{vendor_column: relative_path}` for the stored files; the route then
`setattr(vendor, column, path)`.

**All-or-nothing:** the first bad attachment raises `MigrationAttachmentError`,
which the route turns into a 400 and rolls back the whole vendor — safer than a
vendor with half its documents.

## Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | `process_vendor_attachments` orchestrator + `MigrationAttachmentError` export |
| `constants.py` | `VENDOR_FIELD_TO_DOC_TYPE` and related mappings |
| `mapper.py` | `vendor_target(field)` → `(column, folder)` |
| `decoder.py` | `decode_attachment` — Base64 → `FileStorage`, mime sniff |
| `uploader.py` | `store(...)` — wraps the real `upload_file` service |
| `validators.py` | `validate_entry`, `validate_ext`, `MigrationAttachmentError` |

## Why a separate package
Keeping decode/validate/store out of `routes.py` keeps the route readable and lets
the same logic be reused/tested in isolation. It also means the migration uses the
**production** upload path (same folder layout, naming, size/ext guards), so
documents are consistent with normal uploads.
