"""Unit tests for Brønnøysund API response normalization and client with mocked HTTP."""

from datetime import timezone
import httpx
import pytest

from app.services.brreg_client import (
    BrregClient,
    BrregHttpError,
    BrregInvalidOrgNrError,
    BrregMalformedDataError,
    BrregNetworkError,
    BrregNotFoundError,
    BrregOrgMismatchError,
    BrregTimeoutError,
)


@pytest.fixture
def sample_api_response():
    """Sample authentic Enhetsregisteret API response for testing."""
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
            "kommune": "STAVANGER",
            "kommunenummer": "1103",
            "land": "Norge",
            "landkode": "NO"
        },
        "postadresse": {
            "adresse": ["Postboks 8500"],
            "postnummer": "4035",
            "poststed": "STAVANGER",
            "land": "Norge",
            "landkode": "NO"
        },
        "konkurs": False,
        "underAvvikling": False,
        "underTvangsavviklingEllerTvangsopplosning": False,
        "antallAnsatte": 21000,
        "hjemmeside": "www.equinor.com",
        "registrertIMvaregisteret": True,
        "sisteInnsendteAarsregnskap": "2024",
        "institusjonellSektorkode": {
            "kode": "2100",
            "beskrivelse": "Private aksjeselskaper mv."
        },
        "stiftelsesdato": "1972-09-18"
    }


def test_normalize_complete_response(sample_api_response):
    """Test normalizing a complete Enhetsregisteret response with expanded fields."""
    client = BrregClient()
    profile, evidence = client.normalize_response(sample_api_response)

    assert profile.org_nr == "923609016"
    assert profile.name == "EQUINOR ASA"
    assert profile.org_form == "ASA"
    assert profile.org_form_description == "Allment aksjeselskap"
    assert profile.registration_date == "1997-09-08"
    assert profile.nace_code == "06.100"
    assert profile.nace_description == "Utvinning av råolje og naturgass"
    assert profile.is_bankrupt is False
    assert profile.is_in_liquidation is False
    assert profile.is_forced_dissolution is False
    assert profile.employees == 21000
    assert profile.official_website == "www.equinor.com"
    assert profile.registered_in_vat is True
    assert profile.latest_submitted_accounts == "2024"
    assert profile.institutional_sector_code == "2100"
    assert profile.institutional_sector_description == "Private aksjeselskaper mv."
    assert profile.municipality == "STAVANGER"
    assert profile.municipality_code == "1103"
    assert profile.foundation_date == "1972-09-18"

    # Check addresses
    assert profile.business_address is not None
    assert profile.business_address.address_lines == ["Forusbeen 50"]
    assert profile.business_address.postal_code == "4035"
    assert profile.business_address.city == "STAVANGER"

    assert profile.postal_address is not None
    assert profile.postal_address.address_lines == ["Postboks 8500"]

    # Check evidence
    assert evidence.source_name == "Brønnøysund Register Centre (Enhetsregisteret)"
    assert evidence.source_url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    assert evidence.retrieved_at.tzinfo == timezone.utc
    assert len(evidence.response_hash) == 64  # SHA-256


def test_normalize_zero_employees_preserved_and_empty_website():
    """Test that antallAnsatte=0 is preserved as 0 (not None), and empty website becomes None."""
    payload = {
        "organisasjonsnummer": "999111222",
        "navn": "Zero Staff AS",
        "antallAnsatte": 0,
        "hjemmeside": "   ",
        "registrertIMvaregisteret": False,
    }
    client = BrregClient()
    profile, _ = client.normalize_response(payload)
    assert profile.employees == 0
    assert profile.official_website is None
    assert profile.registered_in_vat is False



def test_normalize_distinguishes_missing_vs_empty_values():
    """Verify that absent fields evaluate to None rather than being inferred as empty or inactive."""
    minimal_raw = {
        "organisasjonsnummer": "999888777",
        "navn": "MINIMAL ENK",
        # Notice: no organisasjonsform, no addresses, no bankruptcy/liquidation flags
    }

    client = BrregClient()
    profile, evidence = client.normalize_response(minimal_raw)

    assert profile.org_nr == "999888777"
    assert profile.name == "MINIMAL ENK"
    assert profile.org_form is None
    assert profile.business_address is None
    assert profile.postal_address is None
    assert profile.nace_code is None

    # Crucial rule: Status must not be inferred as false/inactive if unmentioned in registry
    assert profile.is_bankrupt is None
    assert profile.is_in_liquidation is None
    assert profile.is_deleted is None


def test_mocked_http_fetch(sample_api_response):
    """
    Verify rule: Use mocked HTTP responses in tests so ordinary test runs
    never depend on network availability.
    """
    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "923609016" in str(request.url)
        return httpx.Response(200, json=sample_api_response)

    transport = httpx.MockTransport(mock_handler)
    mock_client = httpx.Client(transport=transport)

    client = BrregClient(http_client=mock_client)
    profile, evidence = client.fetch_company("923609016")

    assert profile.org_nr == "923609016"
    assert profile.name == "EQUINOR ASA"
    assert evidence.status_code == 200
    assert evidence.response_hash == client.compute_payload_hash(sample_api_response)


def test_fetch_company_invalid_org_number():
    """Verify that fetch_company rejects invalid org numbers before making network calls."""
    client = BrregClient()
    for invalid in ["12345", "ABC123456", "1234567890"]:
        with pytest.raises(BrregInvalidOrgNrError):
            client.fetch_company(invalid)


def test_fetch_company_not_found():
    """Verify handling of HTTP 404 (organization not found)."""
    def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"feilmelding": "Ikke funnet"})

    transport = httpx.MockTransport(not_found_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(BrregNotFoundError) as exc_info:
        client.fetch_company("999999999")
    assert "not found in Enhetsregisteret" in str(exc_info.value)


def test_fetch_company_org_number_mismatch():
    """Verify rule: Validate that returned org number exactly matches requested number."""
    mismatched_payload = {
        "organisasjonsnummer": "111222333",  # Different from requested 923609016
        "navn": "Mismatched AS"
    }

    def mismatch_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mismatched_payload)

    transport = httpx.MockTransport(mismatch_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(BrregOrgMismatchError) as exc_info:
        client.fetch_company("923609016")
    assert "Organization number mismatch" in str(exc_info.value)


def test_fetch_company_timeout():
    """Verify safe handling of connection and read timeouts."""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Connection timed out")

    transport = httpx.MockTransport(timeout_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(BrregTimeoutError) as exc_info:
        client.fetch_company("923609016")
    assert "timed out" in str(exc_info.value)


def test_fetch_company_network_error():
    """Verify safe handling of network/connectivity failures."""
    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Failed to resolve host")

    transport = httpx.MockTransport(error_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(BrregNetworkError) as exc_info:
        client.fetch_company("923609016")
    assert "Network error" in str(exc_info.value)


def test_fetch_company_malformed_json():
    """Verify safe handling of malformed or non-dict JSON responses."""
    def bad_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")

    transport = httpx.MockTransport(bad_json_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(BrregMalformedDataError) as exc_info:
        client.fetch_company("923609016")
    assert "Malformed JSON" in str(exc_info.value)


def test_fetch_company_server_error():
    """Verify safe handling of 5xx HTTP server errors."""
    def server_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(server_error_handler)
    client = BrregClient(http_client=httpx.Client(transport=transport))

    with pytest.raises(BrregHttpError) as exc_info:
        client.fetch_company("923609016")
    assert "HTTP 500" in str(exc_info.value)
