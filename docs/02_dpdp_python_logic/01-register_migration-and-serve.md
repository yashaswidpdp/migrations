# 01 — `register_migration` & `serve.py`

How the migration blueprint gets built and mounted, and how the app is booted so
the `/api/migration/*` routes exist.

## `migration_ext/__init__.py`

```python
migration_bp = Blueprint("migration_ext", __name__)
from . import routes                                  # binds the route decorators
from .source_map import MigrationSourceMap, ensure_source_map_table

def register_migration(app):
    if "migration_ext" not in app.blueprints:
        app.register_blueprint(migration_bp, url_prefix="/api/migration")
    with app.app_context():
        ensure_source_map_table()
    return app
```

- `migration_bp` is created first; `from . import routes` is imported *after* so the
  `@migration_bp.route(...)` decorators bind to it.
- `register_migration(app)`:
  - mounts the blueprint at **`/api/migration`** (idempotent — guarded by the
    `"migration_ext" not in app.blueprints` check, so calling twice is safe);
  - calls `ensure_source_map_table()` to create/upgrade the
    `migration_source_map` table (see `04-source_map-idempotency.md`).
- **Nothing upstream is edited.** The only way this runs is if the serve entrypoint
  calls it.

## `migration_ext/serve.py` — the boot entrypoint

```python
app = create_app()                       # the normal Flask app
register_migration(app)                  # mount /api/migration/*
_patch_template_variable_whitelist()     # runtime monkey-patch (see below)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
```

- Adds the dpdp_python root to `sys.path` and `load_dotenv(.env)` **before**
  importing the app (config reads env at import time).
- Builds the standard app via `create_app()`, then `register_migration(app)`.
- Exposes module-level `app`, so gunicorn can serve `migration_ext.serve:app`.

### `_patch_template_variable_whitelist()` — runtime patch, not a core edit
Upstream's `routes/notice_template/crud.validate_template_variables()` hard-codes a
small allow-list (`name, email, company, date, details, contact_info`) and rejects
legacy Odoo placeholders like `{var1}`, which would block migrating templates such
as "MSG91-OTP Template". `serve.py` swaps in a wrapper that also permits the
`var<N>` family by reassigning `crud.validate_template_variables`. Done here (at
boot, in the extension) so it **survives `git pull`** instead of editing the core
file.

## Why booting the right way matters

| Launcher | Loads `/api/migration/*`? |
|---|---|
| `python -m migration_ext.serve` | ✅ yes |
| `gunicorn 'migration_ext.serve:app'` | ✅ yes |
| `python app.py`, `gunicorn app:app`, `start_app.py`, `run.py` | ❌ no — routes 404 |

If every migration load 404s while core routes (`/consent`, `/stakeholder`) work,
the app was booted the wrong way — restart via `migration_ext.serve`.
