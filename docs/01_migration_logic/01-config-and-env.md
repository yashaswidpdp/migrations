# 01 — Configuration & Environment

All runtime config lives in **`migration/config/.env`** (gitignored;
`config/.env.example` is the committed template). Every module loads it via
`load_dotenv("config/.env")` at import, so **commands must be run from the
`migration/` directory** (the path is relative).

## Keys

### Source (Odoo) — read-only
| Key | Meaning |
|---|---|
| `ODOO_BASE_URL` | Odoo API base, e.g. `https://tech.portal-uat.dpdpconsultants.com/api` |
| `ODOO_JWT_TOKEN` | Bearer token for Odoo. **Expires in hours** — refresh before a big run. |
| `ODOO_SESSION_ID` | Odoo `session_id` cookie (some endpoints require both JWT + cookie). |

### Destination (Flask) — write target
| Key | Meaning |
|---|---|
| `FLASK_API_BASE_URL` | e.g. `http://localhost:5000/api` |
| `FLASK_API_KEY` | Bearer token for Flask. Expires fast; re-run on 401 (idempotent). |
| `FLASK_TENANT_DOMAIN` | Sent as the `Host` header so Flask resolves the right tenant (e.g. `skfinance.localhost.com`). |

### Throughput knobs
| Key | Default | Effect |
|---|---|---|
| `BATCH_SIZE` | 50 (`.env` uses 500) | dashboard page size (`rec_limit`) during extract |
| `ENRICH_WORKERS` | 16 | thread-pool width for **extraction** (parallel pagination + by-id enrichment). Lower to 8 if Odoo 429/503s. |
| `LOAD_WORKERS` | 8 | thread-pool width for **consent/request loads** (sharded by principal/vendor). Drop to 4 on DB contention. |
| `MAX_RECORDS` | 0 (no limit) | `>0` caps extraction for test runs and **forces sequential pagination** so it can stop cleanly. |

### Paths (rarely changed)
| Key | Default |
|---|---|
| `DATA_RAW_DIR` | `data/raw` |
| `DATA_PROCESSED_DIR` | `data/processed` |
| `DATA_DIR` | `data` |
| `LOGS_DIR` | `logs` |
| `LOG_LEVEL` | `INFO` |

## Concurrency: how the two worker knobs differ

- **`ENRICH_WORKERS`** governs *reads* (extraction). These are pure I/O waits, so
  it scales near-linearly; the only ceiling is Odoo's rate limiter, not your CPU.
- **`LOAD_WORKERS`** governs *writes* (consent/request load). Each write commits to
  the DB and touches shared state (data principals, licenses, vendor links), so
  it's kept lower and the work is **sharded** so the same principal/vendor is never
  written concurrently (see `05-load-load_flask.md`).

## Token expiry — the failure mode to recognize

Odoo reports an expired token as an **HTTP-200 envelope**, not a 401:

```json
{"message": "Token Expired", "status_code": 401}
```

The extractor detects this (`extract_odoo.py`) and **aborts loudly** rather than
saving an empty dataset. If a run dies with that message, refresh `ODOO_JWT_TOKEN`.

## Secret hygiene

`config/.env` is gitignored and is the single place tokens live (the reconciler and
loader both read it). Never hardcode tokens in code. Rotate the Odoo JWT + Flask
API key after debugging sessions.
