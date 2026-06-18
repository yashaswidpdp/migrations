"""Build the internal-stakeholder migration report.

The loader hands a list of per-stakeholder result dicts; this module logs a
human-readable block per record (the [SUCCESS]/[FAILED] format from the
migration spec) and writes a CSV + JSON summary under data/processed/ for audit.
"""

import json
import logging
import os

import pandas as pd

logger = logging.getLogger("stakeholder_report")

DATA_PROCESSED_DIR = os.getenv("DATA_PROCESSED_DIR", "data/processed")

# Result `status` values the loader emits.
CREATED = "created"
UPDATED = "updated"
SKIPPED = "skipped"
FAILED = "failed"


def log_result(result: dict):
    """Emit one audit block for a single stakeholder result."""
    status = result.get("status")
    odoo_id = result.get("odoo_source_id")
    email = result.get("email")
    if status == CREATED:
        logger.info(f"[SUCCESS]\nOdoo Stakeholder ID: {odoo_id}\nEmail: {email}\n"
                    f"Created Flask Stakeholder ID: {result.get('flask_user_id')}")
    elif status == UPDATED:
        logger.info(f"[SUCCESS]\nOdoo Stakeholder ID: {odoo_id}\nEmail: {email}\n"
                    f"Updated Existing Stakeholder ID: {result.get('flask_user_id')}")
    elif status == SKIPPED:
        logger.info(f"[SKIPPED]\nOdoo Stakeholder ID: {odoo_id}\nEmail: {email}\n"
                    f"Reason: {result.get('reason')}")
    else:
        logger.error(f"[FAILED]\nOdoo Stakeholder ID: {odoo_id}\nEmail: {email}\n"
                     f"Reason: {result.get('reason')}")


def write_report(results: list, csv_filename: str = "processed_stakeholders.csv"):
    """Write CSV + JSON report and log a final summary. Returns the counts dict."""
    counts = {CREATED: 0, UPDATED: 0, SKIPPED: 0, FAILED: 0}
    for r in results:
        counts[r.get("status", FAILED)] = counts.get(r.get("status", FAILED), 0) + 1

    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
    base = os.path.splitext(csv_filename)[0]
    report_csv = os.path.join(DATA_PROCESSED_DIR, f"report_{base}.csv")
    report_json = os.path.join(DATA_PROCESSED_DIR, f"report_{base}.json")

    if results:
        pd.DataFrame(results).to_csv(report_csv, index=False)
    with open(report_json, "w", encoding="utf-8") as f:
        json.dump({"summary": counts, "results": results}, f, ensure_ascii=False, indent=2)

    logger.info(
        f"Stakeholder migration report: {counts[CREATED]} created, "
        f"{counts[UPDATED]} updated, {counts[SKIPPED]} skipped, "
        f"{counts[FAILED]} failed. Written to {report_csv} and {report_json}."
    )
    return counts
