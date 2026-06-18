# Memory Index

- [Attachment storage convention](attachment-storage-convention.md) — backend has no Attachment table; path-string columns + uploads/<resource>/; reuse upload_file
- [Stakeholder migration](stakeholder-migration.md) — Odoo /stakeholders -> Flask /stakeholder/create; map roles by name (gap RESOLVED: DPO alias + PA Manager role id=3)
- [Vendor migration](vendor-migration.md) — Odoo /vendors_details -> /migration/vendor; phone-collision email-only retry; test02 same-email conflict accepted unmigrated (11/12)
- [Reconciliation audit](reconciliation-audit.md) — `python main.py reconcile`; tracked 76.4%; 123 license-blocked + 6 silently-dropped consents; PA/templates untracked
