# Odoo to Flask Migration Workspace

This repository is dedicated to the migration process from a legacy Odoo instance to a modern Flask application. It follows a modular ETL (Extract, Transform, Load) architecture.

## 📂 Project Structure & Code Files

- **`main.py`**: The CLI orchestrator. Wires together extraction, transformation, and loading into unified commands (e.g., `run-consent-pipeline`, `run-request-pipeline`).
- **`config/.env.example`**: Template for environment variables (Odoo JWT tokens, Session IDs, Flask API keys).
- **`docs/mapping.md`**: Official documentation of field-by-field and enum value mappings from Odoo to Flask.
- **`agents.md`**: Memory Context Protocol file used to track agent tasks, architectural decisions, and bug fixes across sessions.

### ETL Scripts (`scripts/`)
- **`scripts/extract/extract_odoo.py`**: Handles HTTP POST requests to Odoo's custom APIs. Manages dual authentication (JWT + Session Cookie), offset pagination, dynamic JSON parsing, and saves raw data to CSV.
- **`scripts/transform/transform_consent.py`**: Contains the transformation logic for Consents (`dpcmData`). Safely parses nested arrays, maps Odoo's legacy statuses/types to strict Flask Enums, and drops the OTP requirement for migration.
- **`scripts/transform/transform_request.py`**: Contains the transformation logic for Data Subject Requests/Grievances (`dpgrData`). Aligns Odoo's 'Not Assigned'/'Assigned To DPO' statuses with Flask's Initiation tracking and extracts integer IDs from complex lists.
- **`scripts/load/load_flask.py`**: Reads the processed CSVs and sequentially POSTs records to the new Flask API, logging successes and writing failed rows to an error CSV for idempotency tracking.

## 🚀 Getting Started

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environments**:
   Copy `config/.env.example` to `config/.env` and fill in your actual tokens.

3. **Run Pipelines**:
   The migration is orchestrated via `main.py`. The CLI is designed around intuitive entity groups: `consent` and `request`.

   **Method 1: Run Full Pipelines (All Stages Together)**
   This will automatically extract from Odoo, transform the data, split the streams, and load into Flask.
   ```bash
   # Run full Consent migration
   python main.py consent run-all
   
   # Run full Request migration
   python main.py request run-all
   ```

   **Method 2: Run Stage-by-Stage (Granular)**
   Use these commands if you want to inspect the data manually between stages.
   
   *Stage 1: Extract from Odoo*
   ```bash
   # Extracts to data/raw/
   python main.py consent extract
   python main.py request extract
   ```
   
   *Stage 1.5: Transform Data*
   ```bash
   # Transforms and maps Enums to data/processed/
   python main.py consent transform
   python main.py request transform
   ```
   
   *Stage 2: Load into Flask*
   ```bash
   # Automatically splits and loads Consents to /import (deemed) and /live-consent (live)
   python main.py consent load
   
   # Loads Requests to /request/create
   python main.py request load
   ```

# claude --resume "fix-odoo-extractor-api"
# claude --resume "split-consent-loading-strategy"