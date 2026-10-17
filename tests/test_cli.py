"""Automated unit tests for CLI lookup and error cases using mocked HTTP responses."""

from datetime import timezone
import httpx
import pytest

from app.main import init_database, lookup_company
from app.services.brreg_client import BrregClient
from app.services.database import DatabaseService


@pytest.fixture
def mock_equinor_response():
    """Mock API response for Equinor ASA."""
    return {
        "organisasjonsnummer": "923609016",
        "navn": "EQUINOR ASA",
        "organisasjonsform": {
            "kode": "ASA",
            "beskrivelse": "Allment aksjeselskap"
        },
        "registreringsdatoEnhetsregisteret": "1997-09-08",
        "naeringskode1": {
            "kode": "06.100",
            "beskrivelse": "Utvinning av råolje og naturgass"
        },
        "forretningsadresse": {
            "adresse": ["Forusbeen 50"],
            "postnummer": "4035",
            "poststed": "STAVANGER",
            "land": "Norge",
            "landkode": "NO"
        },
        "konkurs": False,
        "underAvvikling": False
    }


def test_cli_init_db(tmp_path, capsys):
    """Test database initialization from CLI."""
    db_path = tmp_path / "cli_init.db"
    exit_code = init_database(db_path)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Database tables, indexes, and foreign keys successfully initialized" in captured.out
    assert db_path.exists()


def test_cli_lookup_success(tmp_path, mock_equinor_response, capsys):
    """Test successful CLI lookup using mocked HTTP response."""
    db_path = tmp_path / "cli_test.db"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_equinor_response)

    transport = httpx.MockTransport(mock_handler)
    mock_client = BrregClient(http_client=httpx.Client(transport=transport))

    exit_code = lookup_company("923609016", db_path=db_path, client=mock_client)
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Official Company Profile: EQUINOR ASA" in captured.out
    assert "Organization Number: 923609016" in captured.out
    assert "Legal Form:          ASA (Allment aksjeselskap)" in captured.out
    assert "NACE Industry Code:  06.100" in captured.out
    assert "Business Address:    Forusbeen 50, 4035 STAVANGER, Norge" in captured.out
    assert "Saved as Version 1 in SQLite" in captured.out

    # Verify persistence in SQLite
    db = DatabaseService(db_path)
    profile = db.get_company_profile("923609016")
    assert profile is not None
    assert profile.name == "EQUINOR ASA"


def test_cli_lookup_update_tracking(tmp_path, mock_equinor_response, capsys):
    """Test CLI lookup with update tracking when company data changes."""
    db_path = tmp_path / "cli_update.db"

    # Step 1: Initial lookup
    transport1 = httpx.MockTransport(lambda req: httpx.Response(200, json=mock_equinor_response))
    client1 = BrregClient(http_client=httpx.Client(transport=transport1))
    lookup_company("923609016", db_path=db_path, client=client1)

    # Step 2: Company updates name
    updated_payload = dict(mock_equinor_response)
    updated_payload["navn"] = "EQUINOR ENERGY ASA"

    transport2 = httpx.MockTransport(lambda req: httpx.Response(200, json=updated_payload))
    client2 = BrregClient(http_client=httpx.Client(transport=transport2))
    exit_code = lookup_company("923609016", db_path=db_path, client=client2)
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "Saved as Version 2 in SQLite" in captured.out
    assert "1 field(s) updated from previous version" in captured.out
    assert "name: 'EQUINOR ASA' -> 'EQUINOR ENERGY ASA'" in captured.out


def test_cli_lookup_invalid_org_number(tmp_path, capsys):
    """Test CLI lookup rejecting invalid organization numbers."""
    db_path = tmp_path / "cli_invalid.db"
    exit_code = lookup_company("12345", db_path=db_path)
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Validation Error" in captured.err
    assert "Must be exactly 9 digits" in captured.err or "must be exactly 9 digits" in captured.err


def test_cli_lookup_not_found(tmp_path, capsys):
    """Test CLI lookup handling 404 Not Found."""
    db_path = tmp_path / "cli_notfound.db"

    def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not found"})

    transport = httpx.MockTransport(not_found_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    exit_code = lookup_company("999999999", db_path=db_path, client=client)
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Registry Lookup Error" in captured.err
    assert "not found in Enhetsregisteret" in captured.err


def test_cli_lookup_org_mismatch(tmp_path, capsys):
    """Test CLI lookup detecting returned org number mismatch."""
    db_path = tmp_path / "cli_mismatch.db"

    def mismatch_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organisasjonsnummer": "111222333", "navn": "Other AS"})

    transport = httpx.MockTransport(mismatch_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    exit_code = lookup_company("923609016", db_path=db_path, client=client)
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Data Integrity Error" in captured.err
    assert "Organization number mismatch" in captured.err


def test_cli_lookup_timeout(tmp_path, capsys):
    """Test CLI lookup handling timeout gracefully."""
    db_path = tmp_path / "cli_timeout.db"

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Request timed out")

    transport = httpx.MockTransport(timeout_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    exit_code = lookup_company("923609016", db_path=db_path, client=client)
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "Timeout Error" in captured.err
