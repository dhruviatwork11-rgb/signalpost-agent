"""Unit tests for SQLite persistence, foreign key enforcement, transactions, and snapshots."""

import sqlite3
import pytest

from app.models import (
    Address,
    CompanyProfile,
    SourceEvidence,
    utc_now,
)
from app.services.database import DatabaseService, get_connection


@pytest.fixture
def db(tmp_path):
    """Fixture providing a fresh isolated SQLite database for each test."""
    db_file = tmp_path / "test_signalpost.db"
    return DatabaseService(db_file)


def test_foreign_key_enforcement(db):
    """Verify that PRAGMA foreign_keys = ON is enforced and blocks orphaned child rows."""
    conn = db.get_connection()
    try:
        # Check that pragma is indeed 1 (ON)
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys;")
        assert cursor.fetchone()[0] == 1

        # Attempt to insert source_evidence for a non-existent company org_nr
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                cursor.execute(
                    """
                    INSERT INTO source_evidence (
                        org_nr, source_name, source_url, retrieved_at, response_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "999999999",  # Does not exist in companies table
                        "Test Registry",
                        "https://example.com/test",
                        utc_now().isoformat(),
                        "dummyhash",
                    ),
                )
    finally:
        conn.close()


def test_initial_company_creation_and_snapshot(db):
    """Verify transactional creation of company, source evidence, and initial version 1 snapshot."""
    profile = CompanyProfile(
        org_nr="923609016",
        name="Equinor ASA",
        org_form="ASA",
        org_form_description="Allment aksjeselskap",
        registration_date="1997-09-08",
        business_address=Address(
            address_lines=["Forusbeen 50"],
            postal_code="4035",
            city="STAVANGER",
        ),
        nace_code="06.100",
        nace_description="Utvinning av råolje og naturgass",
        is_bankrupt=False,
        is_in_liquidation=False,
    )
    evidence = SourceEvidence(
        source_name="Brønnøysund Register Centre",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=utc_now(),
        response_hash="hash_v1",
        status_code=200,
    )

    version, changes = db.upsert_company_profile(profile, evidence)
    assert version == 1
    assert len(changes) == 0  # No changes on initial creation

    # Verify retrieval
    stored = db.get_company_profile("923609016")
    assert stored is not None
    assert stored.name == "Equinor ASA"
    assert stored.org_nr == "923609016"
    assert stored.is_bankrupt is False

    # Verify snapshot
    snapshots = db.get_snapshots("923609016")
    assert len(snapshots) == 1
    assert snapshots[0].version == 1
    assert snapshots[0].snapshot_data["name"] == "Equinor ASA"
    assert snapshots[0].snapshot_data["nace_code"] == "06.100"

    # Verify source evidence
    evidence_records = db.get_source_evidence("923609016")
    assert len(evidence_records) == 1
    assert evidence_records[0].response_hash == "hash_v1"


def test_versioned_update_and_change_history(db):
    """Verify that updates create version 2 snapshots and track field-level changes."""
    # Step 1: Insert initial profile
    p1 = CompanyProfile(
        org_nr="984851006",
        name="DNB ASA",
        org_form="ASA",
        business_address=Address(
            address_lines=["Stranden 21"],
            postal_code="0250",
            city="OSLO",
        ),
    )
    e1 = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/984851006",
        retrieved_at=utc_now(),
        response_hash="hash_1",
    )
    db.upsert_company_profile(p1, e1)

    # Step 2: Company changes name and address
    p2 = CompanyProfile(
        org_nr="984851006",
        name="DNB Bank ASA",  # Changed name
        org_form="ASA",
        business_address=Address(
            address_lines=["Dronning Eufemias gate 30"],  # Changed address
            postal_code="0191",
            city="OSLO",
        ),
    )
    e2 = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/984851006",
        retrieved_at=utc_now(),
        response_hash="hash_2",
    )

    version, changes = db.upsert_company_profile(p2, e2)
    assert version == 2
    assert len(changes) == 2

    changed_fields = {c.field_name for c in changes}
    assert "name" in changed_fields
    assert "business_address" in changed_fields

    # Verify both snapshots are preserved
    snapshots = db.get_snapshots("984851006")
    assert len(snapshots) == 2
    assert snapshots[0].version == 1
    assert snapshots[0].snapshot_data["name"] == "DNB ASA"
    assert snapshots[1].version == 2
    assert snapshots[1].snapshot_data["name"] == "DNB Bank ASA"

    # Verify change history table
    history = db.get_change_history("984851006")
    assert len(history) == 2
    name_change = next(c for c in history if c.field_name == "name")
    assert name_change.old_value == "DNB ASA"
    assert name_change.new_value == "DNB Bank ASA"
    assert name_change.version == 2


def test_transaction_rollback_on_failure(db):
    """Verify that any database failure during an upsert cleanly rolls back the transaction."""
    conn = db.get_connection()
    try:
        # Create an initial valid company
        profile = CompanyProfile(
            org_nr="923609016",
            name="Equinor ASA",
        )
        evidence = SourceEvidence(
            source_name="Enhetsregisteret",
            source_url="https://example.com",
            response_hash="test_hash",
        )
        db.upsert_company_profile(profile, evidence)

        # Confirm 1 snapshot exists
        assert len(db.get_snapshots("923609016")) == 1

        # Simulate a transaction failure midway (e.g. invalid snapshot data or foreign key failure)
        # Verify that companies table was not partially updated
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                cursor = conn.cursor()
                # Update company name
                cursor.execute(
                    "UPDATE companies SET name = 'Corrupted Name' WHERE org_nr = '923609016'"
                )
                # Intentionally cause a foreign key violation to abort the transaction
                cursor.execute(
                    """
                    INSERT INTO source_evidence (
                        org_nr, source_name, source_url, retrieved_at, response_hash
                    ) VALUES ('000000000', 'Bad', 'Bad', '2026-01-01', 'h')
                    """
                )
    finally:
        conn.close()

    # Re-verify that the rollback left the original company name intact
    fresh_profile = db.get_company_profile("923609016")
    assert fresh_profile.name == "Equinor ASA"


def test_expanded_field_change_tracking_and_idempotency(db):
    """Verify that employee changes, website updates, and VAT changes are audited, while identical runs are idempotent."""
    org_nr = "923609016"
    p1 = CompanyProfile(
        org_nr=org_nr,
        name="Tech Innovators AS",
        employees=15,
        official_website="https://tech.no",
        registered_in_vat=False,
        latest_submitted_accounts="2022",
        municipality="TRONDHEIM",
        municipality_code="5001",
    )
    e1 = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}",
        response_hash="hash_p1",
    )
    v1, ch1 = db.upsert_company_profile(p1, e1)
    assert v1 == 1
    assert ch1 == []

    # Idempotent re-run with exact same profile
    v1_repeat, ch1_repeat = db.upsert_company_profile(p1, e1)
    assert v1_repeat == 1
    assert ch1_repeat == []

    # Update profile: employee count grew, website updated, registered in VAT, new accounts
    p2 = CompanyProfile(
        org_nr=org_nr,
        name="Tech Innovators AS",
        employees=42,  # Changed from 15 to 42
        official_website="https://innovators.no",  # Changed website
        registered_in_vat=True,  # Changed to True
        latest_submitted_accounts="2023",  # Changed accounts year
        municipality="TRONDHEIM",
        municipality_code="5001",
    )
    e2 = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}",
        response_hash="hash_p2",
    )
    v2, ch2 = db.upsert_company_profile(p2, e2)
    assert v2 == 2
    assert len(ch2) == 4

    diff_fields = {c.field_name: (c.old_value, c.new_value) for c in ch2}
    assert diff_fields["employees"] == ("15", "42")
    assert diff_fields["official_website"] == ("https://tech.no", "https://innovators.no")
    assert diff_fields["registered_in_vat"] == ("0", "1")
    assert diff_fields["latest_submitted_accounts"] == ("2022", "2023")

    # Stored profile reflects latest values
    latest = db.get_company_profile(org_nr)
    assert latest.employees == 42
    assert latest.official_website == "https://innovators.no"
    assert latest.registered_in_vat is True
    assert latest.latest_submitted_accounts == "2023"
    assert latest.municipality == "TRONDHEIM"

