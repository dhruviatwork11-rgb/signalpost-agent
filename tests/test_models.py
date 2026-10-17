"""Unit tests for Pydantic domain models and validation rules."""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from app.models import Address, CompanyProfile, SourceEvidence, utc_now


def test_valid_org_number():
    """Verify that 9-digit Norwegian organization numbers are accepted and cleaned."""
    profile = CompanyProfile(
        org_nr="923609016",
        name="Equinor ASA",
    )
    assert profile.org_nr == "923609016"

    # With whitespace
    profile_ws = CompanyProfile(
        org_nr=" 923 609 016 ",
        name="Equinor ASA",
    )
    assert profile_ws.org_nr == "923609016"


def test_invalid_org_numbers():
    """Verify that malformed organization numbers are rejected."""
    invalid_cases = [
        "12345678",       # 8 digits
        "1234567890",     # 10 digits
        "ABC123456",      # Alphanumeric
        "",               # Empty
        "---------",      # Punctuation
    ]
    for case in invalid_cases:
        with pytest.raises(ValidationError):
            CompanyProfile(org_nr=case, name="Test Company")


def test_status_fields_default_to_none():
    """
    Verify rule: Company registration status is represented only when supported by source data.
    Do not infer that a company is inactive merely because a field is missing.
    """
    profile = CompanyProfile(
        org_nr="984851006",
        name="DNB Bank ASA",
    )
    assert profile.is_bankrupt is None
    assert profile.is_in_liquidation is None
    assert profile.is_deleted is None


def test_explicit_status_fields():
    """Verify that explicit status flags can be set when registry provides them."""
    profile = CompanyProfile(
        org_nr="984851006",
        name="DNB Bank ASA",
        is_bankrupt=False,
        is_in_liquidation=False,
        is_deleted=False,
    )
    assert profile.is_bankrupt is False
    assert profile.is_in_liquidation is False
    assert profile.is_deleted is False


def test_source_evidence_utc_enforcement():
    """Ensure SourceEvidence forces timestamps to UTC."""
    naive_dt = datetime(2026, 10, 16, 12, 0, 0)
    evidence = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=naive_dt,
        response_hash="abc123hash",
    )
    assert evidence.retrieved_at.tzinfo == timezone.utc


def test_address_formatting():
    """Verify single-line formatting of structured address."""
    addr = Address(
        address_lines=["Forusbeen 50"],
        postal_code="4035",
        city="STAVANGER",
        country="Norge",
    )
    formatted = addr.format_single_line()
    assert formatted == "Forusbeen 50, 4035 STAVANGER, Norge"


def test_expanded_profile_fields_and_zero_employees():
    """Verify expanded fields, including preserving 0 employees as distinct from missing."""
    profile_with_zero = CompanyProfile(
        org_nr="923609016",
        name="Holding AS",
        employees=0,
        official_website="https://holding.no",
        registered_in_vat=False,
        latest_submitted_accounts="2023",
        institutional_sector_code="2100",
        institutional_sector_description="Private aksjeselskaper",
        municipality="BERGEN",
        municipality_code="4601",
        is_forced_dissolution=False,
        foundation_date="2020-01-15",
    )
    assert profile_with_zero.employees == 0
    assert profile_with_zero.official_website == "https://holding.no"
    assert profile_with_zero.registered_in_vat is False
    assert profile_with_zero.latest_submitted_accounts == "2023"
    assert profile_with_zero.municipality == "BERGEN"
    assert profile_with_zero.foundation_date == "2020-01-15"


def test_deterministic_summary_synthesis():
    """Verify deterministic synthesis without fabrication."""
    profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        org_form="ASA",
        org_form_description="Allment aksjeselskap",
        registration_date="1997-09-08",
        nace_code="06.100",
        nace_description="Utvinning av råolje",
        municipality="STAVANGER",
        employees=21000,
        registered_in_vat=True,
        latest_submitted_accounts="2024",
        official_website="www.equinor.com",
    )
    summary = profile.generate_summary()
    assert "EQUINOR ASA (org nr: 923609016)" in summary
    assert "Allment aksjeselskap" in summary
    assert "based in STAVANGER" in summary
    assert "active and in normal standing" in summary
    assert "21000 registered employees" in summary
    assert "registered in the VAT register" in summary
    assert "latest submitted annual accounts for 2024" in summary
    assert "website: www.equinor.com" in summary

    # In liquidation test
    distressed = CompanyProfile(
        org_nr="999888777",
        name="Distressed AS",
        is_in_liquidation=True,
    )
    distressed_summary = distressed.generate_summary()
    assert "in liquidation" in distressed_summary

