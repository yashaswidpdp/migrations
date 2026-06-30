# 03 — Transform: shared conventions

The transform layer (`scripts/transform/transform_*.py`) reads `data/raw/*` and
writes `data/processed/*`, mapping Odoo's schema/field-names/enums onto exactly
what the Flask migration endpoints expect. There is **one module per entity**
(documented individually in `04-transform-per-entity.md`); this file covers the
conventions they all share so the per-entity doc can stay focused.

## Single responsibility

A transform does **field shaping only** — rename, enum-map, date-normalize,
extract nested values, drop noise. It never calls Flask and never decides
idempotency (that's the loader + source-map). Output is a flat CSV (or JSON for
tree/structured entities) keyed by column names the loader reads.

## Conventions every transform follows

### 1. Phone as string
Raw CSVs are read with `pd.read_csv(path, dtype={"phone": str})`. Without this,
pandas infers a blank-bearing phone column as `float64`, turning `'9878987819'`
into `9878987819.0` and dropping leading zeros — and the `.0` would reach Flask.

### 2. Dates: normalize, never fabricate
Source dates are normalized to a consistent string the backend parser accepts
(requests emit ISO `YYYY-MM-DDTHH:MM:SS`; consents emit `dd/mm/YYYY`). **Time-of-day
is preserved** end-to-end so historical chronology survives. A missing/garbage
source date becomes empty → the backend stores NULL rather than inventing `now()`.

### 3. NaN guards
pandas represents blanks as float `NaN`. Transforms (and the loader) convert these
to `None`/empty so a literal `'nan'` string never reaches Postgres. Watch for the
"NaN-revert trap": a date column with mixed blanks can round-trip through pandas as
the string `'nan'` — guard with `pd.isna(...)`.

### 4. Enum mapping
Odoo's free-text/loose values are mapped to the exact Flask enum *values* (e.g.
consent status `"deemed"` → `"Deemed Consent"`). See each transform's `map_*`
helpers. The Flask enum vocabularies are documented inline at the top of each
transform (e.g. `transform_consent.py` lists `ConsentStatus`, `LegacyTypeEnum`,
`ConsentTypeEnum`, `ProcessingTypeEnum`).

### 5. Nested extraction
Odoo embeds related objects as `[{id, name}]` arrays or dict-strings. Transforms
extract the needed scalar(s) and emit either resolvable **names** (the loader maps
names→Flask ids server-side) or raw JSON the backend parses.

## Where the output goes

| Entity | Processed file |
|---|---|
| request type | `processed_request_types.json` |
| stakeholder | `processed_stakeholders.csv` |
| processing activity | `processed_processing_activities.csv` |
| template | `processed_templates.csv` |
| vendor | `processed_vendors.csv` |
| consent | `processed_consents.csv` |
| request | `processed_requests.csv` |

The loader (`05-load-load_flask.md`) reads these and POSTs one row per record.
