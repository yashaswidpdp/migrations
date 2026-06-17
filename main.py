import click
import logging
import os
import sys
from dotenv import load_dotenv
from scripts.extract.extract_odoo import (
    run_extraction,
    run_pa_extraction,
    run_template_extraction,
    run_request_enrichment,
    run_consent_enrichment,
)
from scripts.transform.transform_consent import transform_consent_data
from scripts.transform.transform_request import transform_request_data
from scripts.transform.transform_processing_activity import transform_processing_activity_data
from scripts.transform.transform_template import transform_template_data
from scripts.load.load_flask import (
    run_loading,
    run_request_type_seeding,
    run_legacy_loading,
    run_paper_loading,
    run_consent_migration_loading,
    run_pa_loading,
    run_pa_link_patch,
    run_template_loading,
    run_template_approval,
    run_template_load_and_approve,
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

        click.echo("Stage 1.8: Seeding request types (idempotent)...")
        run_request_type_seeding("request_types_seed.json")

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


if __name__ == "__main__":
    cli()
