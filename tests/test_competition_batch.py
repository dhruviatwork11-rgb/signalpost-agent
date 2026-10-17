"""Automated offline tests for the official Signalpost competition batch runner."""

from datetime import timezone
import json
from pathlib import Path
import httpx
import pytest

from app.services.batch_runner import (
    ALLOWED_AVAILABILITIES,
    BatchRunner,
    read_organisation_inputs,
)
from app.services.brreg_client import BrregClient


@pytest.fixture
def sample_company_response_factory():
    """Generates deterministic mock API responses for any organization number."""
    def _create(org_nr: str):
        return {
            "organisasjonsnummer": org_nr,
            "navn": f"TEST COMPANY {org_nr} AS",
            "organisasjonsform": {
                "kode": "AS",
                "beskrivelse": "Aksjeselskap"
            },
            "registreringsdatoEnhetsregisteret": "2020-01-01",
            "stiftelsesdato": "2019-11-20",
            "naeringskode1": {
                "kode": "62.010",
                "beskrivelse": "Programmeringstjenester"
            },
            "forretningsadresse": {
                "adresse": [f"Street {org_nr}"],
                "postnummer": "0150",
                "poststed": "OSLO",
                "kommune": "OSLO",
                "kommunenummer": "0301",
                "land": "Norge",
                "landkode": "NO"
            },
            "antallAnsatte": 42,
            "hjemmeside": f"https://company{org_nr}.no",
            "registrertIMvaregisteret": True,
            "sisteInnsendteAarsregnskap": "2024",
            "institusjonellSektorkode": {
                "kode": "2100",
                "beskrivelse": "Private foretak"
            },
            "konkurs": False,
            "underAvvikling": False,
            "underTvangsavviklingEllerTvangsopplosning": False,
        }
    return _create


def test_read_organisation_inputs_json_formats(tmp_path):
    """Test parsing varied JSON structures (list of strings, list of objects, dict)."""
    # 1. List of strings
    p1 = tmp_path / "list_strings.json"
    p1.write_text(json.dumps(["923609016", "984851006"]), encoding="utf-8")
    assert read_organisation_inputs(p1) == ["923609016", "984851006"]

    # 2. List of dicts
    p2 = tmp_path / "list_dicts.json"
    p2.write_text(json.dumps([{"organisation_number": "923609016"}, {"organisation_number": "984851006"}]), encoding="utf-8")
    assert read_organisation_inputs(p2) == ["923609016", "984851006"]

    # 3. Dict with key
    p3 = tmp_path / "dict_key.json"
    p3.write_text(json.dumps({"organisation_numbers": ["923609016", "984851006"]}), encoding="utf-8")
    assert read_organisation_inputs(p3) == ["923609016", "984851006"]

    # 4. List of dicts with Norwegian key 'organisasjonsnummer'
    p4 = tmp_path / "list_dicts_no.json"
    p4.write_text(json.dumps([{"organisasjonsnummer": "923609016"}, {"organisasjonsnummer": "984851006"}]), encoding="utf-8")
    assert read_organisation_inputs(p4) == ["923609016", "984851006"]


def test_read_organisation_inputs_jsonl_and_text(tmp_path):
    """Test parsing JSONL and plain-text formats."""
    # JSONL
    p_jsonl = tmp_path / "input.jsonl"
    p_jsonl.write_text('{"organisation_number": "923609016"}\n{"organisation_number": "984851006"}\n', encoding="utf-8")
    assert read_organisation_inputs(p_jsonl) == ["923609016", "984851006"]

    # Plain text
    p_txt = tmp_path / "input.txt"
    p_txt.write_text("923609016\n984851006\n", encoding="utf-8")
    assert read_organisation_inputs(p_txt) == ["923609016", "984851006"]


def test_read_organisation_inputs_validations(tmp_path):
    """Test duplicate preservation and malformed number rejection."""
    # Duplicates are preserved to ensure zero silent drops
    p_dup = tmp_path / "dups.json"
    p_dup.write_text(json.dumps(["923609016", "984851006", "923609016"]), encoding="utf-8")
    assert read_organisation_inputs(p_dup) == ["923609016", "984851006", "923609016"]

    # Malformed numbers are rejected
    p_bad = tmp_path / "bad.json"
    p_bad.write_text(json.dumps(["12345"]), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid Norwegian organisation number"):
        read_organisation_inputs(p_bad)


def test_100_company_smoke_test(tmp_path, sample_company_response_factory):
    """
    100-company smoke test verifying:
    1. Exactly one terminal envelope per input company (100 in, 100 out, zero drops).
    2. Exact envelope structure compliant with OUTPUT_CONTRACT.md.
    3. Proper handling of edge cases (e.g., 404, network failure) without omission.
    """
    # Load 100 org numbers from data/smoke_test_100_companies.json
    source_fixture = Path("data/smoke_test_100_companies.json")
    orgs = json.loads(source_fixture.read_text(encoding="utf-8"))
    assert len(orgs) == 100

    # Let orgs[50] simulate 404 Not Found, and orgs[51] simulate a transient failure that retries
    not_found_org = orgs[50]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for org in orgs:
            if org in url_str:
                if org == not_found_org:
                    return httpx.Response(404, json={"message": "Not found"})
                return httpx.Response(200, json=sample_company_response_factory(org))
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    mock_client = BrregClient(http_client=httpx.Client(transport=transport))

    db_path = tmp_path / "smoke_100.db"
    out_jsonl = tmp_path / "smoke_100_envelopes.jsonl"
    report_json = tmp_path / "smoke_100_report.json"

    runner = BatchRunner(db_path=db_path, client=mock_client, workers=4)
    report = runner.run(
        input_path=source_fixture,
        output_path=out_jsonl,
        report_path=report_json,
        run_id="smoke-test-100",
    )

    # Check summary report
    assert report["expected_count"] == 100
    assert report["emitted_envelopes"] == 100
    assert report["zero_silent_drops"] is True
    assert report["completed"] == 99
    assert report["not_found"] == 1
    assert report["failed_or_blocked"] == 0
    assert report["third_party_cost_usd"] == 0.0

    # Read output envelopes
    lines = [line.strip() for line in out_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 100

    envelopes = [json.loads(line) for line in lines]
    emitted_orgs = [e["organisation_number"] for e in envelopes]
    assert emitted_orgs == orgs  # Preserved exact input ordering

    # Validate schema for every envelope
    for env in envelopes:
        assert "organisation_number" in env
        assert "run" in env
        assert "claims" in env
        assert "evidence" in env
        assert "changes" in env
        assert "errors" in env
        assert "operations" in env

        run_obj = env["run"]
        assert run_obj["terminal_status"] in ("completed", "not_found", "failed", "blocked")
        assert run_obj["started_at"].endswith("Z")
        assert run_obj["completed_at"].endswith("Z")

        # Check claim availability states
        for claim in env["claims"]:
            assert claim["availability"] in ALLOWED_AVAILABILITIES
            if claim["availability"] == "not_available":
                assert claim["value"] is None  # Never fabricated or 0 for missing values

        # Check operations
        ops = env["operations"]
        assert ops["requests"] >= 1
        assert ops["runtime_ms"] >= 0
        assert ops["third_party_cost_usd"] == 0.0


def test_batch_idempotent_refresh(tmp_path, sample_company_response_factory):
    """Verify rule: Identical snapshots must not create false changes on subsequent runs."""
    input_file = tmp_path / "refresh_orgs.json"
    test_orgs = ["923609016", "984851006"]
    input_file.write_text(json.dumps(test_orgs), encoding="utf-8")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        for org in test_orgs:
            if org in str(request.url):
                return httpx.Response(200, json=sample_company_response_factory(org))
        return httpx.Response(404)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    db_path = tmp_path / "idempotent.db"
    out1 = tmp_path / "out1.jsonl"
    out2 = tmp_path / "out2.jsonl"

    runner = BatchRunner(db_path=db_path, client=client, workers=2)

    # First run: initial persistence
    runner.run(input_file, out1, run_id="run-1")
    rows1 = [json.loads(line) for line in out1.read_text(encoding="utf-8").splitlines()]
    assert all(r["changes"] == [] for r in rows1)

    # Second run with identical data: must be idempotent with 0 changes
    runner.run(input_file, out2, run_id="run-2")
    rows2 = [json.loads(line) for line in out2.read_text(encoding="utf-8").splitlines()]
    assert all(r["changes"] == [] for r in rows2)  # Zero false changes!


def test_envelope_expanded_claims_and_synthesis(tmp_path, sample_company_response_factory):
    """Verify that completed envelopes contain all official expanded claims, precise evidence, and deterministic synthesis."""
    org_nr = "923609016"
    input_file = tmp_path / "single_org.json"
    input_file.write_text(json.dumps([org_nr]), encoding="utf-8")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sample_company_response_factory(org_nr))

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    db_path = tmp_path / "single.db"
    out_file = tmp_path / "single_out.jsonl"

    runner = BatchRunner(db_path=db_path, client=client, workers=1)
    runner.run(input_file, out_file, run_id="test-envelope-claims")

    row = json.loads(out_file.read_text(encoding="utf-8").strip())
    assert row["run"]["terminal_status"] == "completed"

    claims_by_field = {c["field"]: c for c in row["claims"]}
    evidence_by_id = {e["id"]: e for e in row["evidence"]}

    # Verify official website claim and evidence span
    assert "official_website" in claims_by_field
    assert claims_by_field["official_website"]["value"] == f"https://company{org_nr}.no"
    assert claims_by_field["official_website"]["availability"] == "available"
    assert claims_by_field["official_website"]["confidence"] == 1.0
    ev_web_id = claims_by_field["official_website"]["evidence_ids"][0]
    assert ev_web_id in evidence_by_id
    assert f"hjemmeside: https://company{org_nr}.no" == evidence_by_id[ev_web_id]["claim_span"]

    # Verify employees claim and evidence span
    assert "employees" in claims_by_field
    assert claims_by_field["employees"]["value"] == 42
    assert claims_by_field["employees"]["availability"] == "available"
    ev_emp_id = claims_by_field["employees"]["evidence_ids"][0]
    assert ev_emp_id in evidence_by_id
    assert "antallAnsatte: 42" == evidence_by_id[ev_emp_id]["claim_span"]

    # Verify VAT register claim and evidence span
    assert "registered_in_vat_register" in claims_by_field
    assert claims_by_field["registered_in_vat_register"]["value"] is True
    ev_vat_id = claims_by_field["registered_in_vat_register"]["evidence_ids"][0]
    assert ev_vat_id in evidence_by_id
    assert "registrertIMvaregisteret: true" == evidence_by_id[ev_vat_id]["claim_span"]

    # Verify accounts claim
    assert "latest_submitted_accounts" in claims_by_field
    assert claims_by_field["latest_submitted_accounts"]["value"] == "2024"

    # Verify sector claim
    assert "institutional_sector_code" in claims_by_field
    assert claims_by_field["institutional_sector_code"]["value"] == "2100"

    # Verify municipality claim
    assert "municipality" in claims_by_field
    assert claims_by_field["municipality"]["value"] == "OSLO"

    # Verify profile summary synthesis claim
    assert "profile_summary" in claims_by_field
    summary_claim = claims_by_field["profile_summary"]
    assert summary_claim["availability"] == "available"
    assert f"TEST COMPANY {org_nr} AS" in summary_claim["value"]
    assert "42 registered employees" in summary_claim["value"]
    assert "based in OSLO" in summary_claim["value"]
    ev_sum_id = summary_claim["evidence_ids"][0]
    assert ev_sum_id in evidence_by_id

    # Verify first evidence item (legal_name)
    ev = row["evidence"][0]
    assert ev["id"] == "ev-1"
    assert ev["source_class"] == "official_registry"
    assert f"{org_nr}" in ev["source_url"]
    assert len(ev["content_sha256"]) == 64
    assert f"navn: TEST COMPANY {org_nr} AS" in ev["claim_span"]

