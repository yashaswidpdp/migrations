# 09 — Performance, parallelism & resume

This consolidates the speed work across extract and load, the knobs that control
it, and the safety caveats. It cross-references `02-extract-extract_odoo.md` and
`05-load-load_flask.md`.

## The original problem

At 15k–100k records a naive run took ~3 hours. Two phases dominated:
1. **Dashboard pagination** — sequential page-by-page GETs.
2. **By-id enrichment** — one `GET /…/id?id=N` per record (the N+1).
3. **Loads** — one POST per row, sequential, plus per-call TLS setup.

## What was done

### Extraction (read) — controlled by `ENRICH_WORKERS` (default 16)
- **Pooled keep-alive session** with retry/backoff (kills per-call TLS handshakes).
- **Parallel pagination** — page 1 learns `total_page`, pages `2..N` fetched
  concurrently. Sequential fallback when `total_page` is unknown or `MAX_RECORDS`
  is set.
- **Parallel by-id enrichment** with a **resumable JSONL checkpoint**
  (`raw_*.csv.enrich.jsonl`): a killed run resumes from where it stopped; the
  checkpoint is deleted only after the enriched CSV is written.
- *Result:* dashboard pull ~8s; full enrich of 15k ~7 min (scales down further by
  raising `ENRICH_WORKERS`). It's pure I/O wait, so CPU is never the limit — only
  Odoo's rate limiter is.

### Loads (write) — controlled by `LOAD_WORKERS` (default 8)
- **Pooled session, `max_retries=0`** (never auto-resend a possibly-committed POST).
- **Sharded parallelism** — `_shard_by_keys` keeps the same principal (consents)
  or principal+vendor (requests) serial, while distinct shards run concurrently.
- *Result:* 15,151 consents in ~5.5 min (was ~50 min).

## The knobs

| Knob | Phase | Default | Tune |
|---|---|---|---|
| `ENRICH_WORKERS` | extract | 16 | raise (32/48) if Odoo tolerates; lower to 8 on 429/503 |
| `LOAD_WORKERS` | load | 8 | drop to 4 on DB contention |
| `MAX_RECORDS` | extract | 0 | `>0` for test runs (forces sequential paging) |

## Caveats (read before a big run)

1. **Refresh `ODOO_JWT_TOKEN` first** — it expires in hours. Expiry is an HTTP-200
   envelope; the extractor aborts loudly. Mid-run expiry → re-run resumes from the
   checkpoint.
2. **Rate limit / WAF** — raise `ENRICH_WORKERS` until *Odoo* pushes back (429/503),
   not until your CPU does. Start at 8 if unsure, ramp up.
3. **Write safety rests on the shard keys** — consents are safe if principals are
   keyed by phone/email; requests additionally key vendors. If the backend ever
   dedups principals by another field (e.g. name), revisit the shard keys.
4. **No-contact rows serialize** — requests with neither phone nor email share one
   dummy identity → one serial shard (correct, but no speedup for that subset).

## Possible future wins (not yet built)

- **Persistent cross-run enrichment cache** — keep the by-id checkpoint across
  runs (keyed by Odoo id) so repeated `migrate-all` runs skip enrichment entirely
  during a frozen migration window. Add a `--refresh` to force a re-pull.
- **Bulk by-id endpoint** — if Odoo ever accepts `id=1,2,3,…`, the N+1 collapses to
  ~N/100 calls.
