import click
import logging
import os
import sys
from dotenv import load_dotenv
from scripts.extract.extract_odoo import (
    run_extraction,
    run_pa_extraction,
    run_template_extraction,
    run_vendor_extraction,
    run_stakeholder_extraction,
    run_request_enrichment,
    run_consent_enrichment,
    run_request_type_extraction,
)
from scripts.transform.transform_consent import transform_consent_data
from scripts.transform.transform_request import transform_request_data
from scripts.transform.transform_processing_activity import transform_processing_activity_data
from scripts.transform.transform_template import transform_template_data
from scripts.transform.transform_vendor import transform_vendor_data
from scripts.transform.transform_stakeholder import transform_stakeholder_data
from scripts.transform.transform_request_type import transform_request_type_data
from scripts.load.load_flask import (
    run_loading,
    run_request_type_seeding,
    run_request_type_loading,
    run_legacy_loading,
    run_paper_loading,
    run_consent_migration_loading,
    run_pa_loading,
    run_pa_link_patch,
    run_template_loading,
    run_template_approval,
    run_template_load_and_approve,
    run_template_pa_link_patch,
    run_vendor_loading,
    run_stakeholder_loading,
)

# Setup logging
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        # mode="a" => append; log persists across runs until file is deleted
        logging.FileHandler("logs/migration.log", mode="a"),
        logging.StreamHandler()
    ],
    # force=True => override any basicConfig already set by imported modules,
    # otherwise the FileHandler silently never attaches and the file stays empty
    force=True,
)
logger = logging.getLogger("migration")

@click.group()
@click.pass_context
def cli(ctx):
    """Odoo to Flask Migration Orchestrator"""
    load_dotenv("config/.env")
    # Timestamped banner per command run; appended to logs/migration.log
    command = " ".join(sys.argv[1:]) or "<no command>"
    logger.info("=" * 60)
    logger.info("COMMAND RUN: %s", command)
    logger.info("=" * 60)

# ==========================================
# CONSENT COMMANDS
# ==========================================
@cli.group()
def consent():
    """Commands for migrating Consents (DPCM)"""
    pass

@consent.command()
def extract():
    """Stage 1: Extract from Odoo to data/raw/"""
    logger.info("Starting consent extraction...")
    try:
        run_extraction("/dpcm/dashboard", "raw_consents.csv")
        click.echo("Successfully extracted data to data/raw/raw_consents.csv")
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)

@consent.command()
def enrich():
    """Stage 1.2: Backfill type fields per record via GET /dpcm/id?id=<N>"""
    logger.info("Starting consent enrichment via /dpcm/id...")
    try:
        run_consent_enrichment("raw_consents.csv")
        click.echo("Consent enrichment complete (userActivityType/type fields backfilled).")
    except Exception as e:
        logger.error(f"Enrichment failed: {e}")
        click.echo(f"Error: {e}", err=True)

@consent.command()
def transform():
    """Stage 1.5: Transform data to Flask format"""
    logger.info("Starting consent transformation...")
    try:
        transform_consent_data("raw_consents.csv", "processed_consents.csv")
        click.echo("Transformation complete. Check data/processed/processed_consents.csv")
    except Exception as e:
        logger.error(f"Transformation failed: {e}")
        click.echo(f"Error: {e}", err=True)

@consent.command()
def load():
    """Stage 2: Split & Load data into Flask API"""
    logger.info("Starting consent load via migration extension...")
    try:
        click.echo("Loading consents (paper + legacy, dates preserved) → /migration/consent...")
        run_consent_migration_loading("processed_consents.csv")
        click.echo("Consent loading complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Loading failed: {e}")
        click.echo(f"Error: {e}", err=True)

@consent.command()
def run_all():
    """Run full pipeline: Extract → Transform → Load"""
    logger.info("Starting full consent migration pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_extraction("/dpcm/dashboard", "raw_consents.csv")

        click.echo("Stage 1.2: Enriching type fields via /dpcm/id...")
        run_consent_enrichment("raw_consents.csv")

        click.echo("Stage 1.5: Transforming...")
        transform_consent_data("raw_consents.csv", "processed_consents.csv")

        click.echo("Stage 2: Loading consents (paper + legacy, dates preserved) → /migration/consent...")
        run_consent_migration_loading("processed_consents.csv")

        click.echo("Full consent pipeline completed.")
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)

# ==========================================
# REQUEST TYPE COMMANDS  (master data — run before consents + requests)
# ==========================================
@cli.group(name="request-type")
def request_type():
    """Commands for migrating Request Types (Odoo /request-types -> Flask /request-types/create)"""
    pass


@request_type.command()
def extract():
    """Stage 1: Extract request types from Odoo -> data/raw/raw_request_types.json"""
    logger.info("Starting request-type extraction...")
    try:
        run_request_type_extraction("raw_request_types.json")
        click.echo("Extracted to data/raw/raw_request_types.json")
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)


@request_type.command()
def transform():
    """Stage 1.5: Rename Odoo fields -> Flask -> data/processed/processed_request_types.json"""
    logger.info("Starting request-type transformation...")
    try:
        transform_request_type_data("raw_request_types.json", "processed_request_types.json")
        click.echo("Transformation complete. Check data/processed/processed_request_types.json")
    except Exception as e:
        logger.error(f"Transformation failed: {e}")
        click.echo(f"Error: {e}", err=True)


@request_type.command()
def load():
    """Stage 2: Load request types into Flask (idempotent by name)"""
    logger.info("Starting request-type load...")
    try:
        run_request_type_loading("processed_request_types.json")
        click.echo("Request-type loading complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Loading failed: {e}")
        click.echo(f"Error: {e}", err=True)


@request_type.command(name="run-all")
def request_type_run_all():
    """Run full pipeline: Extract -> Transform -> Load. Run before consents + requests."""
    logger.info("Starting full request-type migration pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_request_type_extraction("raw_request_types.json")
        click.echo("Stage 1.5: Transforming...")
        transform_request_type_data("raw_request_types.json", "processed_request_types.json")
        click.echo("Stage 2: Loading...")
        run_request_type_loading("processed_request_types.json")
        click.echo("Full request-type migration completed.")
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)


# ==========================================
# REQUEST COMMANDS
# ==========================================
@cli.group()
def request():
    """Commands for migrating Requests (DPGR)"""
    pass

@request.command()
def extract():
    """Stage 1: Extract from Odoo to data/raw/"""
    logger.info("Starting request extraction...")
    try:
        run_extraction("/dpgr/dashboard", "raw_requests.csv")
        click.echo("Successfully extracted data to data/raw/raw_requests.csv")
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)

@request.command()
def enrich():
    """Stage 1.2: Add requestType + assignee email per record via GET /dpgr/id?id=<N>"""
    logger.info("Starting request enrichment via /dpgr/id...")
    try:
        run_request_enrichment("raw_requests.csv")
        click.echo("Request enrichment complete (requestType + assignee_email added).")
    except Exception as e:
        logger.error(f"Enrichment failed: {e}")
        click.echo(f"Error: {e}", err=True)

@request.command()
@click.option("--user-id", "user_id", type=int, default=None, help="Flask user_id to set as assigned_users on every request")
def transform(user_id):
    """Stage 1.5: Transform data to Flask format"""
    logger.info("Starting request transformation...")
    try:
        transform_request_data("raw_requests.csv", "processed_requests.csv", assigned_user_id=user_id)
        click.echo("Transformation complete. Check data/processed/processed_requests.csv")
    except Exception as e:
        logger.error(f"Transformation failed: {e}")
        click.echo(f"Error: {e}", err=True)

@request.command()
def seed_types():
    """Stage 1.8: Seed Flask request_types the migrated requests refer to (idempotent)"""
    logger.info("Seeding request types from data/request_types_seed.json...")
    try:
        run_request_type_seeding("request_types_seed.json")
        click.echo("Request-type seeding complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Seeding failed: {e}")
        click.echo(f"Error: {e}", err=True)

@request.command()
def load():
    """Stage 2: Load data into Flask API"""
    logger.info("Starting request load...")
    try:
        run_loading("processed_requests.csv", "/migration/request")
        click.echo("Request loading complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Loading failed: {e}")
        click.echo(f"Error: {e}", err=True)

@request.command()
@click.option("--user-id", "user_id", type=int, default=None, help="Flask user_id to set as assigned_users on every request")
def run_all(user_id):
    """Run full pipeline: Extract → Transform → Load"""
    logger.info("Starting full request migration pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_extraction("/dpgr/dashboard", "raw_requests.csv")

        click.echo("Stage 1.2: Enriching requestType via /dpgr/id...")
        run_request_enrichment("raw_requests.csv")

        click.echo("Stage 1.5: Transforming...")
        transform_request_data("raw_requests.csv", "processed_requests.csv", assigned_user_id=user_id)

        click.echo("Stage 1.8: Loading request types from Odoo extract (idempotent)...")
        run_request_type_extraction("raw_request_types.json")
        transform_request_type_data("raw_request_types.json", "processed_request_types.json")
        run_request_type_loading("processed_request_types.json")

        click.echo("Stage 2: Loading...")
        run_loading("processed_requests.csv", "/migration/request")
        
        click.echo("Full request migration completed successfully.")
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)

# ==========================================
# PROCESSING ACTIVITY COMMANDS
# ==========================================
@cli.group()
def processing_activity():
    """Commands for migrating Processing Activities (master data)"""
    pass


@processing_activity.command()
def extract():
    """Stage 1: Extract PA tree from Odoo → data/raw/raw_processing_activities.json"""
    logger.info("Starting Processing Activity extraction...")
    try:
        run_pa_extraction("raw_processing_activities.json")
        click.echo("Extracted to data/raw/raw_processing_activities.json")
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)


@processing_activity.command()
def transform():
    """Stage 1.5: Flatten PA tree → data/processed/processed_processing_activities.csv"""
    logger.info("Starting Processing Activity transformation...")
    try:
        transform_processing_activity_data(
            "raw_processing_activities.json",
            "processed_processing_activities.csv"
        )
        click.echo("Transformation complete. Check data/processed/processed_processing_activities.csv")
    except Exception as e:
        logger.error(f"Transformation failed: {e}")
        click.echo(f"Error: {e}", err=True)


@processing_activity.command()
def load():
    """Stage 2: Load PAs into Flask (parents first, idempotent)"""
    logger.info("Starting Processing Activity load...")
    try:
        run_pa_loading("processed_processing_activities.csv")
        click.echo("PA loading complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Loading failed: {e}")
        click.echo(f"Error: {e}", err=True)


@processing_activity.command()
def patch_links():
    """Backfill template links + effective-from onto PAs already in Flask.

    The load pass skips existing PAs, so their template/date columns stay NULL.
    Run this after templates + PAs are loaded to wire them up (idempotent)."""
    logger.info("Starting Processing Activity link patch...")
    try:
        run_pa_link_patch("processed_processing_activities.csv")
        click.echo("PA link patch complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Link patch failed: {e}")
        click.echo(f"Error: {e}", err=True)


@processing_activity.command()
def run_all():
    """Run full PA pipeline: Extract → Transform → Load"""
    logger.info("Starting full Processing Activity pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_pa_extraction("raw_processing_activities.json")

        click.echo("Stage 1.5: Transforming...")
        transform_processing_activity_data(
            "raw_processing_activities.json",
            "processed_processing_activities.csv"
        )

        click.echo("Stage 2: Loading...")
        run_pa_loading("processed_processing_activities.csv")

        click.echo("Stage 3: Patching template links...")
        run_pa_link_patch("processed_processing_activities.csv")

        click.echo("Full PA pipeline completed.")
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)


# ==========================================
# TEMPLATE COMMANDS
# ==========================================
@cli.group()
def template():
    """Commands for migrating Templates (email/consent/SMS templates)"""
    pass


@template.command()
@click.option("--type", "template_type", default=None, help="Filter by Odoo template type (consent, privacy, email, sms, online)")
def extract(template_type):
    """Stage 1: Extract templates from Odoo → data/raw/raw_templates.json"""
    logger.info("Starting Template extraction...")
    try:
        run_template_extraction("raw_templates.json", template_type=template_type)
        click.echo("Extracted to data/raw/raw_templates.json")
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)


@template.command()
def transform():
    """Stage 1.5: Transform templates → data/processed/processed_templates.csv"""
    logger.info("Starting Template transformation...")
    try:
        transform_template_data("raw_templates.json", "processed_templates.csv")
        click.echo("Transformation complete. Check data/processed/processed_templates.csv")
    except Exception as e:
        logger.error(f"Transformation failed: {e}")
        click.echo(f"Error: {e}", err=True)


@template.command()
def load():
    """Stage 2: Load templates into Flask as Draft (idempotent by name)"""
    logger.info("Starting Template load...")
    try:
        run_template_loading("processed_templates.csv")
        click.echo("Template loading complete (Draft). Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Loading failed: {e}")
        click.echo(f"Error: {e}", err=True)


@template.command()
def approve():
    """Stage 2.5: Activate loaded Draft templates (PUT approval=Active)"""
    logger.info("Starting Template approval...")
    try:
        run_template_approval("processed_templates.csv")
        click.echo("Template approval complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Approval failed: {e}")
        click.echo(f"Error: {e}", err=True)


@template.command(name="patch-pa-links")
def patch_pa_links():
    """Backfill processing-activity links onto templates already in Flask.

    Re-runnable + idempotent. Use after PAs are loaded (or after fixing PA
    resolution) to wire template<->PA links without re-creating templates."""
    logger.info("Starting Template PA-link patch...")
    try:
        run_template_pa_link_patch("processed_templates.csv")
        click.echo("Template PA-link patch complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Template PA-link patch failed: {e}")
        click.echo(f"Error: {e}", err=True)


@template.command(name="load-approve")
def load_approve():
    """Stage 2: Load then approve templates in one run (reuses id stash)"""
    logger.info("Starting Template load + approve...")
    try:
        run_template_load_and_approve("processed_templates.csv")
        click.echo("Template load + approve complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Load+approve failed: {e}")
        click.echo(f"Error: {e}", err=True)


@template.command()
def run_all():
    """Run full template pipeline: Extract → Transform → Load → Approve"""
    logger.info("Starting full Template pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_template_extraction("raw_templates.json")

        click.echo("Stage 1.5: Transforming...")
        transform_template_data("raw_templates.json", "processed_templates.csv")

        # Load creates templates as Draft (approval=False); the approve pass
        # PUTs approval=True + status=Active so they become effective and
        # effective_from persists (create ignores both unless approval=True).
        click.echo("Stage 2: Loading + approving...")
        run_template_load_and_approve("processed_templates.csv")

        click.echo("Full template pipeline completed.")
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)


# ==========================================
# VENDOR COMMANDS
# ==========================================
@cli.group()
def vendor():
    """Commands for migrating Vendors (/api/vendors_details)"""
    pass


@vendor.command()
def extract():
    """Stage 1: Extract vendors from Odoo to data/raw/"""
    logger.info("Extracting vendors...")
    try:
        run_vendor_extraction("raw_vendors.json")
        click.echo("Extracted to data/raw/raw_vendors.json")
    except Exception as e:
        logger.error(f"Vendor extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)


@vendor.command()
def transform():
    """Stage 1.5: Transform vendors to Flask format"""
    try:
        transform_vendor_data("raw_vendors.json", "processed_vendors.csv")
        click.echo("Transformation complete. Check data/processed/processed_vendors.csv")
    except Exception as e:
        logger.error(f"Vendor transform failed: {e}")
        click.echo(f"Error: {e}", err=True)


@vendor.command()
def load():
    """Stage 2: Load vendors into Flask API"""
    try:
        run_vendor_loading("processed_vendors.csv")
        click.echo("Vendor loading complete. Check logs/migration.log for details.")
    except Exception as e:
        logger.error(f"Vendor load failed: {e}")
        click.echo(f"Error: {e}", err=True)


@vendor.command(name="run-all")
def vendor_run_all():
    """Run full pipeline: Extract → Transform → Load"""
    logger.info("Starting full vendor migration pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_vendor_extraction("raw_vendors.json")
        click.echo("Stage 1.5: Transforming...")
        transform_vendor_data("raw_vendors.json", "processed_vendors.csv")
        click.echo("Stage 2: Loading...")
        run_vendor_loading("processed_vendors.csv")
        click.echo("Full vendor migration completed.")
    except Exception as e:
        logger.error(f"Vendor pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)


# ==========================================
# INTERNAL STAKEHOLDER COMMANDS
# ==========================================
@cli.group()
def stakeholder():
    """Commands for migrating Internal Stakeholders (/stakeholders -> /stakeholder/create)"""
    pass


@stakeholder.command()
def extract():
    """Stage 1: Extract internal stakeholders from Odoo to data/raw/"""
    logger.info("Extracting internal stakeholders...")
    try:
        run_stakeholder_extraction("raw_stakeholders.json")
        click.echo("Extracted to data/raw/raw_stakeholders.json")
    except Exception as e:
        logger.error(f"Stakeholder extraction failed: {e}")
        click.echo(f"Error: {e}", err=True)


@stakeholder.command()
def transform():
    """Stage 1.5: Transform stakeholders to Flask format"""
    try:
        transform_stakeholder_data("raw_stakeholders.json", "processed_stakeholders.csv")
        click.echo("Transformation complete. Check data/processed/processed_stakeholders.csv")
    except Exception as e:
        logger.error(f"Stakeholder transform failed: {e}")
        click.echo(f"Error: {e}", err=True)


@stakeholder.command()
def load():
    """Stage 2: Load stakeholders into Flask API (role-name mapped, idempotent by email)"""
    try:
        run_stakeholder_loading("processed_stakeholders.csv")
        click.echo("Stakeholder loading complete. See data/processed/report_processed_stakeholders.* and logs/migration.log.")
    except Exception as e:
        logger.error(f"Stakeholder load failed: {e}")
        click.echo(f"Error: {e}", err=True)


@stakeholder.command(name="run-all")
def stakeholder_run_all():
    """Run full pipeline: Extract -> Transform -> Load"""
    logger.info("Starting full stakeholder migration pipeline")
    try:
        click.echo("Stage 1: Extracting...")
        run_stakeholder_extraction("raw_stakeholders.json")
        click.echo("Stage 1.5: Transforming...")
        transform_stakeholder_data("raw_stakeholders.json", "processed_stakeholders.csv")
        click.echo("Stage 2: Loading...")
        run_stakeholder_loading("processed_stakeholders.csv")
        click.echo("Full stakeholder migration completed.")
    except Exception as e:
        logger.error(f"Stakeholder pipeline failed: {e}")
        click.echo(f"Error: {e}", err=True)


@cli.command()
@click.option("--no-write", is_flag=True, help="Print only; do not write the .txt report file.")
@click.option("--self-test", "self_test", is_flag=True, help="Run internal consistency checks and exit.")
@click.option("--live", is_flag=True, help="Verify against live Odoo (SOURCE) + live Flask app (MIGRATED); surfaces DRIFT. Reads tokens from config/.env.")
@click.option("--cached-source", "cached_source", is_flag=True, help="With --live: take SOURCE from the data/raw snapshot instead of re-pulling Odoo (skips the full re-extract; field-diff + DRIFT still run live against Flask).")
def reconcile(no_write, self_test, live, cached_source):
    """Audit Odoo->Flask completeness: per-entity source vs migrated ledger."""
    import scripts.report.reconcile as recon
    from scripts.report.reconcile import run_reconciliation, self_test as _st, REPORT_PATH
    if live:
        recon.LIVE = True
        if not (recon.FLASK_API_BASE_URL and recon.FLASK_API_KEY):
            click.echo("WARN: --live set but FLASK_API_BASE_URL / FLASK_API_KEY missing in "
                       "config/.env; dest verification will read n/a.", err=True)
    if cached_source:
        recon.CACHED_SOURCE = True
        if not live:
            click.echo("NOTE: --cached-source only affects --live runs.", err=True)
    if self_test:
        problems = _st()
        click.echo("SELF-TEST: " + ("PASS" if not problems else "FAIL"))
        for p in problems:
            click.echo(f"  - {p}")
        raise SystemExit(1 if problems else 0)
    report = run_reconciliation(write=not no_write)
    click.echo(report)
    if not no_write:
        click.echo(f"\n[written] {REPORT_PATH}")


# ==========================================
# FULL MIGRATION ORCHESTRATOR
# One command, dependency-ordered, end-to-end. Kept self-contained: it reuses
# the same run_* pipeline functions the per-entity `run-all` commands use, so it
# never drifts from them.
# ==========================================
@cli.command(name="migrate-all")
@click.option("--user-id", "user_id", type=int, default=None,
              help="Flask user_id set as assigned_users on every request (passed to the request stage).")
@click.option("--continue-on-error", is_flag=True,
              help="Keep going if a stage fails. Default: abort, since later stages depend on earlier ones.")
def migrate_all(user_id, continue_on_error):
    """Run the ENTIRE migration end-to-end, in dependency order:

      1. request-type   2. stakeholder   3. processing-activity
      4. templates (+ PA<->template link backfill)   5. vendors
      6. consents (DPCM)   7. requests (DPGR)

    Each entity runs extract -> transform -> load (the same pipelines as the
    per-entity `run-all`). Idempotent: safe to re-run — completed rows 409-skip.

    PREREQUISITES (not done here): a running Flask app booted via
    `migration_ext.serve`, and licenses seeded for the tenant
    (`python -m migration_ext.ensure_license --tenant <id>`). Without licenses
    the consent/request/vendor stages fail with "No active license".
    """
    import time

    def _request_type():
        run_request_type_extraction("raw_request_types.json")
        transform_request_type_data("raw_request_types.json", "processed_request_types.json")
        run_request_type_loading("processed_request_types.json")

    def _stakeholder():
        run_stakeholder_extraction("raw_stakeholders.json")
        transform_stakeholder_data("raw_stakeholders.json", "processed_stakeholders.csv")
        run_stakeholder_loading("processed_stakeholders.csv")

    def _processing_activity():
        run_pa_extraction("raw_processing_activities.json")
        transform_processing_activity_data("raw_processing_activities.json",
                                           "processed_processing_activities.csv")
        run_pa_loading("processed_processing_activities.csv")
        # PA link-patch is intentionally deferred to the template stage: templates
        # don't exist yet, so patching template refs onto PAs here would no-op.

    def _templates():
        run_template_extraction("raw_templates.json")
        transform_template_data("raw_templates.json", "processed_templates.csv")
        run_template_load_and_approve("processed_templates.csv")
        # Both PAs and templates now exist -> backfill BOTH link directions
        # (template refs onto PAs, PA links onto templates). Idempotent.
        run_pa_link_patch("processed_processing_activities.csv")
        run_template_pa_link_patch("processed_templates.csv")

    def _vendors():
        run_vendor_extraction("raw_vendors.json")
        transform_vendor_data("raw_vendors.json", "processed_vendors.csv")
        run_vendor_loading("processed_vendors.csv")

    def _consents():
        run_extraction("/dpcm/dashboard", "raw_consents.csv")
        run_consent_enrichment("raw_consents.csv")
        transform_consent_data("raw_consents.csv", "processed_consents.csv")
        run_consent_migration_loading("processed_consents.csv")

    def _requests():
        run_extraction("/dpgr/dashboard", "raw_requests.csv")
        run_request_enrichment("raw_requests.csv")
        transform_request_data("raw_requests.csv", "processed_requests.csv", assigned_user_id=user_id)
        # Request types are master data for requests; idempotent reload mirrors
        # `request run-all` so a stand-alone requests stage still resolves types.
        run_request_type_extraction("raw_request_types.json")
        transform_request_type_data("raw_request_types.json", "processed_request_types.json")
        run_request_type_loading("processed_request_types.json")
        run_loading("processed_requests.csv", "/migration/request")

    pipeline = [
        ("request-type", _request_type),
        ("stakeholder", _stakeholder),
        ("processing-activity", _processing_activity),
        ("templates + PA links", _templates),
        ("vendors", _vendors),
        ("consents (DPCM)", _consents),
        ("requests (DPGR)", _requests),
    ]

    bar = "=" * 64
    logger.info("migrate-all: starting full migration (%d stages)", len(pipeline))
    overall = time.time()
    failed = []
    for idx, (name, fn) in enumerate(pipeline, 1):
        click.echo(f"\n{bar}\n### STAGE {idx}/{len(pipeline)}: {name}\n{bar}")
        logger.info("migrate-all stage %d/%d start: %s", idx, len(pipeline), name)
        t0 = time.time()
        try:
            fn()
            click.echo(f"--- stage {idx} '{name}' OK ({time.time() - t0:.1f}s) ---")
            logger.info("migrate-all stage %d done: %s (%.1fs)", idx, name, time.time() - t0)
        except Exception as e:
            failed.append(name)
            logger.exception("migrate-all stage %d FAILED: %s: %s", idx, name, e)
            click.echo(f"!!! stage {idx} '{name}' FAILED: {e}", err=True)
            if not continue_on_error:
                click.echo(f"\nABORTED at stage {idx} '{name}'. Fix the cause and re-run "
                           f"`migrate-all` (idempotent — completed rows 409-skip), or pass "
                           f"--continue-on-error to push past stage failures.", err=True)
                raise SystemExit(1)

    dur = time.time() - overall
    if failed:
        click.echo(f"\nmigrate-all finished WITH FAILURES in: {', '.join(failed)} ({dur:.0f}s total).", err=True)
        raise SystemExit(1)
    click.echo(f"\nmigrate-all COMPLETE — all {len(pipeline)} stages OK ({dur:.0f}s total).")


if __name__ == "__main__":
    cli()
