"""Command-line interface and entry point for Signalpost Agent."""

import argparse
import json
from pathlib import Path
import sys
from typing import Optional

from app.config import DATABASE_PATH
from app.services.batch_runner import BatchRunner
from app.services.brreg_client import (
    BrregClient,
    BrregError,
    BrregHttpError,
    BrregInvalidOrgNrError,
    BrregMalformedDataError,
    BrregNetworkError,
    BrregNotFoundError,
    BrregOrgMismatchError,
    BrregTimeoutError,
)
from app.services.database import DatabaseService


def init_database(db_path: Path | str = DATABASE_PATH) -> int:
    """Initialize the SQLite database with full schema and foreign key enforcement."""
    print(f"Initializing SQLite database at: {db_path}")
    DatabaseService(db_path)
    print("Database tables, indexes, and foreign keys successfully initialized.")
    return 0


def import_mock_data(json_path: Path, db_path: Path | str = DATABASE_PATH) -> int:
    """Import sample/mock company records into SQLite without making network calls."""
    if not json_path.exists():
        print(f"Error: File not found at '{json_path}'.", file=sys.stderr)
        return 1

    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not isinstance(records, list):
        records = [records]

    db = DatabaseService(db_path)
    client = BrregClient()

    print(f"Importing {len(records)} record(s) from {json_path}...")
    for idx, raw in enumerate(records, start=1):
        try:
            profile, evidence = client.normalize_response(raw)
            version, changes = db.upsert_company_profile(profile, evidence)
            if changes:
                print(
                    f"[{idx}/{len(records)}] Updated {profile.org_nr} ({profile.name}) to version {version}. "
                    f"{len(changes)} field(s) changed."
                )
            else:
                print(
                    f"[{idx}/{len(records)}] Saved {profile.org_nr} ({profile.name}) at version {version}."
                )
        except Exception as e:
            print(f"[{idx}/{len(records)}] Failed to import record: {e}", file=sys.stderr)

    print("Import complete.")
    return 0


def view_company(org_nr: str, db_path: Path | str = DATABASE_PATH) -> int:
    """Query and display a local company profile and its audit records from SQLite."""
    db = DatabaseService(db_path)
    profile = db.get_company_profile(org_nr)
    if not profile:
        print(f"No company profile found in local database for organization number: {org_nr}")
        return 1

    snapshots = db.get_snapshots(org_nr)
    changes = db.get_change_history(org_nr)
    evidence_list = db.get_source_evidence(org_nr)

    print("=" * 64)
    print(f"Stored Company Profile: {profile.name} (Org Nr: {profile.org_nr})")
    print("=" * 64)
    print(f"Legal Form:          {profile.org_form or 'Unavailable'} ({profile.org_form_description or 'Unavailable'})")
    print(f"Registration Date:   {profile.registration_date or 'Unavailable'}")
    print(f"Foundation Date:     {profile.foundation_date or 'Unavailable'}")
    print(f"NACE Code:           {profile.nace_code or 'Unavailable'} - {profile.nace_description or 'Unavailable'}")
    print(f"Business Address:    {profile.business_address.format_single_line() if profile.business_address else 'Unavailable'}")
    print(f"Postal Address:      {profile.postal_address.format_single_line() if profile.postal_address else 'Unavailable'}")
    print(f"Municipality:        {profile.municipality or 'Unavailable'} (Code: {profile.municipality_code or 'Unavailable'})")
    print(f"Official Website:    {profile.official_website or 'Unavailable'}")
    print(f"Employees:           {profile.employees if profile.employees is not None else 'Unavailable'}")
    print(f"VAT Registered:      {'Yes' if profile.registered_in_vat is True else 'No' if profile.registered_in_vat is False else 'Not indicated in registry'}")
    print(f"Latest Accounts:     {profile.latest_submitted_accounts or 'Unavailable'}")
    print(f"Sector:              {profile.institutional_sector_code or 'Unavailable'} - {profile.institutional_sector_description or 'Unavailable'}")
    print(f"Bankrupt:            {profile.is_bankrupt if profile.is_bankrupt is not None else 'Not indicated in registry'}")
    print(f"In Liquidation:      {profile.is_in_liquidation if profile.is_in_liquidation is not None else 'Not indicated in registry'}")
    print(f"Forced Dissolution:  {profile.is_forced_dissolution if profile.is_forced_dissolution is not None else 'Not indicated in registry'}")
    print(f"Deregistered/Deleted:{profile.is_deleted if profile.is_deleted is not None else 'Not indicated in registry'}")
    print(f"Executive Summary:   {profile.generate_summary()}")
    print(f"Current Version:     {len(snapshots)}")
    print(f"Total Snapshots:     {len(snapshots)}")
    print(f"Source Queries:      {len(evidence_list)}")

    if evidence_list:
        latest = evidence_list[-1]
        print(f"Latest Evidence:     {latest.source_url} (Retrieved: {latest.retrieved_at.isoformat()})")

    if changes:
        print("\nChange History Audit Trail:")
        for c in changes:
            print(f"  [v{c.version}] {c.field_name}: '{c.old_value}' -> '{c.new_value}' ({c.detected_at.isoformat()})")

    return 0


def lookup_company(
    org_nr: str,
    db_path: Path | str = DATABASE_PATH,
    client: Optional[BrregClient] = None,
) -> int:
    """
    Perform a live registry lookup against the official Brønnøysund Register Centre API,
    verify the returned organization number, save the normalized profile and source evidence
    transactionally to SQLite, and display structured results.
    """
    db = DatabaseService(db_path)
    client = client or BrregClient()

    # Fetch from official API with safe exception categorization
    try:
        profile, evidence = client.fetch_company(org_nr)
    except BrregInvalidOrgNrError as e:
        print(f"Validation Error: {e}", file=sys.stderr)
        return 1
    except BrregNotFoundError as e:
        print(f"Registry Lookup Error: {e}", file=sys.stderr)
        return 1
    except BrregOrgMismatchError as e:
        print(f"Data Integrity Error: {e}", file=sys.stderr)
        return 1
    except BrregTimeoutError as e:
        print(f"Timeout Error: {e}", file=sys.stderr)
        return 1
    except BrregNetworkError as e:
        print(f"Connection Error: {e}", file=sys.stderr)
        return 1
    except BrregMalformedDataError as e:
        print(f"Data Parsing Error: {e}", file=sys.stderr)
        return 1
    except BrregHttpError as e:
        print(f"API Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected Error during registry lookup: {e}", file=sys.stderr)
        return 1

    # Save to SQLite transactionally (profile, evidence, snapshot, change history)
    version, changes = db.upsert_company_profile(profile, evidence)

    # Display verified profile and evidence
    print("=" * 64)
    print(f"Official Company Profile: {profile.name}")
    print("=" * 64)
    print(f"Organization Number: {profile.org_nr}")
    print(f"Legal Form:          {profile.org_form or 'Unavailable'} ({profile.org_form_description or 'Unavailable'})")
    print(f"Registration Date:   {profile.registration_date or 'Unavailable'}")
    print(f"Foundation Date:     {profile.foundation_date or 'Unavailable'}")
    print(f"NACE Industry Code:  {profile.nace_code or 'Unavailable'}")
    print(f"NACE Description:    {profile.nace_description or 'Unavailable'}")
    print(f"Business Address:    {profile.business_address.format_single_line() if profile.business_address else 'Unavailable'}")
    print(f"Postal Address:      {profile.postal_address.format_single_line() if profile.postal_address else 'Unavailable'}")
    print(f"Municipality:        {profile.municipality or 'Unavailable'} (Code: {profile.municipality_code or 'Unavailable'})")
    print(f"Official Website:    {profile.official_website or 'Unavailable'}")
    print(f"Employees:           {profile.employees if profile.employees is not None else 'Unavailable'}")
    print(f"VAT Registered:      {'Yes' if profile.registered_in_vat is True else 'No' if profile.registered_in_vat is False else 'Not indicated in registry'}")
    print(f"Latest Accounts:     {profile.latest_submitted_accounts or 'Unavailable'}")
    print(f"Sector:              {profile.institutional_sector_code or 'Unavailable'} - {profile.institutional_sector_description or 'Unavailable'}")
    print(f"Bankrupt:            {profile.is_bankrupt if profile.is_bankrupt is not None else 'Not indicated in registry'}")
    print(f"In Liquidation:      {profile.is_in_liquidation if profile.is_in_liquidation is not None else 'Not indicated in registry'}")
    print(f"Forced Dissolution:  {profile.is_forced_dissolution if profile.is_forced_dissolution is not None else 'Not indicated in registry'}")
    print(f"Deregistered/Deleted:{profile.is_deleted if profile.is_deleted is not None else 'Not indicated in registry'}")
    print(f"Executive Summary:   {profile.generate_summary()}")
    print("-" * 64)
    print("Source Evidence & Persistence:")
    print(f"Source URL:          {evidence.source_url}")
    print(f"Retrieval Timestamp: {evidence.retrieved_at.isoformat()}")
    print(f"Response SHA-256:    {evidence.response_hash}")
    print(f"Database Record:     Saved as Version {version} in SQLite")
    if changes:
        print(f"Audit Updates:       {len(changes)} field(s) updated from previous version")
        for c in changes:
            print(f"  - {c.field_name}: '{c.old_value}' -> '{c.new_value}'")
    else:
        print("Audit Updates:       No field changes detected (verified current)")
    print("=" * 64)

    return 0


def run_batch_cli(
    input_path: Path | str,
    output_path: Path | str,
    report_path: Optional[Path | str] = None,
    workers: int = 4,
    run_id: Optional[str] = None,
    db_path: Path | str = DATABASE_PATH,
) -> int:
    """Run batch processor compliant with official evaluation contract."""
    try:
        runner = BatchRunner(db_path=db_path, workers=workers)
        report = runner.run(
            input_path=input_path,
            output_path=output_path,
            report_path=report_path,
            run_id=run_id,
        )
        print(f"Batch completed in {report['elapsed_seconds']}s.")
        print(
            f"Emitted: {report['emitted_envelopes']}/{report['expected_count']} terminal envelopes "
            f"(Zero drops: {report['zero_silent_drops']})."
        )
        print(
            f"Completed: {report['completed']}, Not Found: {report['not_found']}, "
            f"Failed/Blocked: {report['failed_or_blocked']}."
        )
        print(f"Output saved to: {output_path}")
        if report_path:
            print(f"Report saved to: {report_path}")
        return 0
    except Exception as e:
        print(f"Batch execution error: {e}", file=sys.stderr)
        return 1


def main() -> None:
    """CLI entry point and argument parser."""
    parser = argparse.ArgumentParser(
        description="Signalpost Agent - Norwegian Company Research & Verification"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db command
    subparsers.add_parser("init-db", help="Initialize SQLite database schema and constraints")

    # lookup command (single live lookup)
    lookup_parser = subparsers.add_parser(
        "lookup", help="Perform live registry lookup against official Brønnøysund API"
    )
    lookup_parser.add_argument("org_nr", help="9-digit Norwegian organization number")

    # batch command (official competition runner)
    batch_parser = subparsers.add_parser(
        "batch", help="Run batch company research on an input list and emit terminal envelopes"
    )
    batch_parser.add_argument(
        "--input", "-i", required=True, help="Input batch file (.json, .jsonl, or .txt)"
    )
    batch_parser.add_argument(
        "--output", "-o", required=True, help="Path for output terminal envelopes JSONL"
    )
    batch_parser.add_argument(
        "--report", "-r", help="Optional path to output machine-readable JSON run report"
    )
    batch_parser.add_argument(
        "--workers", "-w", type=int, default=4, help="Bounded worker concurrency (default 4, max 8)"
    )
    batch_parser.add_argument(
        "--run-id", help="Optional identifier for the run"
    )

    # import-mock command
    import_parser = subparsers.add_parser(
        "import-mock", help="Import offline mock company data into SQLite"
    )
    import_parser.add_argument(
        "file",
        nargs="?",
        default="data/sample_input.json",
        help="Path to JSON file with mock company records",
    )

    # view command
    view_parser = subparsers.add_parser("view", help="View locally stored company details and audit trail")
    view_parser.add_argument("org_nr", help="9-digit organization number")

    args = parser.parse_args()

    exit_code = 0
    if args.command == "init-db":
        exit_code = init_database()
    elif args.command == "lookup":
        exit_code = lookup_company(args.org_nr)
    elif args.command == "batch":
        exit_code = run_batch_cli(
            input_path=args.input,
            output_path=args.output,
            report_path=args.report,
            workers=args.workers,
            run_id=args.run_id,
        )
    elif args.command == "import-mock":
        exit_code = import_mock_data(Path(args.file))
    elif args.command == "view":
        exit_code = view_company(args.org_nr)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
