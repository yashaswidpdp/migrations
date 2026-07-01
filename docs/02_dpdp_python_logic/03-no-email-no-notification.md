# 03 — No emails, no OTP, no notifications (the why & the exact lines)

This is the reason `migration_ext` exists. A **historical backfill must not trigger
the side effects a live action would**: no welcome/credential emails, no consent
notice emails, no OTP, no vendor invite/questionnaire mail, no "request raised"
notifications. The live routes/services fire those; the migration routes
deliberately sit **below that layer**.

All line references are to `dpdp_python/migration_ext/routes.py` unless noted.

## The mechanism, per endpoint

### Consent — direct insert, skipping the notice/email pipeline
The live consent flow builds a notice from a template and emails it (and may run
OTP). `migrate_consent` does **none of that** — it constructs the ORM row directly:

- **routes.py:533–560** — `consent = Consent(...)` then `db.session.add(consent)`.
  No `ConsentService`, no template rendering, no `send_consent_email`, no OTP. The
  module docstring states it: *"POST /api/migration/consent — direct Consent insert
  (no templates/email)."* (routes.py:4).
- Principal creation is also silent: **routes.py:470–483** builds a `User`
  directly and calls only `consume_license(...)` — it sends no account email.

> Net effect: a consent row + its data principal land in the DB with full Odoo
> fidelity, and nobody receives a notice/OTP.

### Stakeholder — backend user with no welcome/reset mail
`migrate_stakeholder` mirrors `POST /stakeholder/create` but drops every mail:

- Docstring (**routes.py:600–604**): *"Create a backend PA Manager user WITHOUT the
  welcome/credential email … no SMTP requirement, no welcome email, no reset-link
  mail."*
- **routes.py:650–653** — it calls `user.set_password(generate_random_password())`
  and `issue_reset_token(user)` so the manager *can* set a password later via the
  normal reset flow, but the inline comment is explicit: *"no email is sent here
  (migration is silent)."* The live route would email the reset link; this one does
  not.

### Vendor — no invite/questionnaire email
`migrate_vendor` mirrors `POST /vendor/create` minus the mail and SMTP requirement:

- Docstring (**routes.py:685–691**): *"Create a Vendor (+ its 'Vendor' contact
  user) WITHOUT any email … drops the questionnaire/invite emails and the SMTP
  requirement."*
- It reuses `_resolve_or_create_vendor_user` (**routes.py:697, 724**) precisely
  because *"it itself sends no email — the real route emails separately"* — so role
  and license behavior match the live path exactly, but no mail goes out.

### Request — reuses the model method, not the emailing route
The live `POST /request/create` sends notifications/OTP as part of raising a
request. `migrate_request` instead calls the **model** method directly:

- **routes.py:175** — `record = Request.create_request(payload, tenant_id,
  **extra_dates)`. The docstring (**routes.py:3**) is explicit: *"POST
  /api/migration/request — reuses Request.create_request (no email)."* The
  email/notification work lives in the *route/service* layer the migration bypasses,
  not in `create_request`.

## Why "below the layer" instead of a flag

The team rule is **glue-only, no core edits** (so `git pull` can't clobber it).
Rather than adding a `send_email=False` flag to core routes (an edit), the
migration *re-implements the create path* against the same models/services that
have no side effects, and reuses shared helpers (`_resolve_or_create_vendor_user`,
`Request.create_request`, `consume_license`) that are themselves mail-free. The
mail/OTP/notification code simply is never on the path.

## License consumption is intentional (not a side effect to suppress)
The migration **does** consume license seats (`consume_license(... "DPCM" ...)` at
routes.py:483; `DPTPA` for vendors) because migrated principals are real users.
That's a deliberate fidelity decision, not a notification. Capacity is provisioned
up front by `ensure_license` (see `05-ensure_license.md`).

## How to verify there were no sends
- No SMTP/`send_*` calls appear on any `/api/migration/*` path (grep the handlers
  above).
- The reset token for stakeholders is *minted* but not *mailed* (routes.py:653).
- Operationally: a migration run produces DB rows + source-map entries and writes to
  `logs/migration.log`; it never enqueues email/Celery notification tasks
  (`migration_ext.serve` runs the web app only — no worker/beat).
