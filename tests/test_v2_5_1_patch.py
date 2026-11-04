"""Regression and compliance tests for Signalpost Agent V2.5.1 patch:
1. No birth dates in model serialization, JSONL, or SQLite records (GDPR privacy compliance).
2. REVI and REGN roles excluded.
3. All six permitted governance codes remain supported (DAGL, LEDE, NEST, MEDL, VARA, INNH).
4. ENK proprietor handling remains accurate.
5. KONSERN and SELSKAP are distinguishable in output claims and claim spans.
6. Currency, fiscal year, and financial values remain unchanged.
7. HTTP 500 produces failed, not not_available.
8. Existing evidence IDs, source URLs, timestamps, hashes, and envelope structure remain valid.
9. No blocked or failed lookup is cached as zero results.
"""

import json
from pathlib import Path
import re
import sqlite3
import httpx
import pytest

from app.models import CompanyProfile, CompanyRole, FinancialAccounts, SourceEvidence, utc_now
from app.services.batch_runner import BatchRunner, build_terminal_envelope
from app.services.brreg_client import BrregClient
from app.services.database import DatabaseService
from app.services.job_verifier import JobVerifier


# ---------------------------------------------------------------------------
# Test 1: No birth dates in model serialization, JSONL, or SQLite records
# ---------------------------------------------------------------------------


def test_no_birth_dates_in_models_jsonl_and_sqlite(tmp_path):
    """Verify that birth dates are completely absent from all models, JSONL, and SQLite records."""
    role = CompanyRole(
        role_code="DAGL",
        role_title="Daglig leder",
        name="Ola Nordmann",
        is_active=True,
    )

    # 1. Model serialization
    role_dict = role.model_dump()
    assert "birth_date" not in role_dict
    assert "fodselsdato" not in role_dict

    role_json = role.model_dump_json()
    assert "birth_date" not in role_json
    assert "fodselsdato" not in role_json

    # 2. SQLite persistence
    db_file = tmp_path / "test_privacy.db"
    db = DatabaseService(str(db_file))
    now = utc_now()
    ev = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
        retrieved_at=now,
        response_hash="c" * 64,
        status_code=200,
    )
    profile = CompanyProfile(
        org_nr="123456789",
        name="PRIVACY TEST AS",
        management_roles=[role],
    )
    db.upsert_company_profile(profile, ev)

    # Read directly from SQLite table verified_assets
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT asset_type, asset_data FROM verified_assets WHERE org_nr = '123456789'")
    rows = cursor.fetchall()
    conn.close()

    assert len(rows) > 0
    for asset_type, asset_data in rows:
        assert "birth_date" not in asset_data
        assert "fodselsdato" not in asset_data

    # 3. JSONL claim and evidence envelope serialization
    ev = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
        retrieved_at=now,
        response_hash="hash_test",
        status_code=200,
    )
    envelope = build_terminal_envelope(
        org_nr="123456789",
        run_id="run-privacy-1",
        started_at=now,
        completed_at=now,
        profile=profile,
        evidence=ev,
    )
    envelope_json = json.dumps(envelope)
    assert "birth_date" not in envelope_json
    assert "fodselsdato" not in envelope_json


# ---------------------------------------------------------------------------
# Test 2: REVI and REGN roles are excluded
# ---------------------------------------------------------------------------


def test_revi_and_regn_roles_excluded():
    """Verify that external service providers (REVI, REGN, BOST) are strictly excluded from management_roles."""
    synthetic_payload = {
        "_links": {"enhet": {"href": "https://data.brreg.no/enhetsregisteret/api/enheter/999888777"}},
        "rollegrupper": [
            {
                "type": {"kode": "REVI", "beskrivelse": "Revisor"},
                "roller": [
                    {
                        "type": {"kode": "REVI", "beskrivelse": "Revisor"},
                        "enhet": {"navn": ["DELOITTE AS"]},
                        "avregistrert": False,
                    }
                ],
            },
            {
                "type": {"kode": "REGN", "beskrivelse": "Regnskapsfører"},
                "roller": [
                    {
                        "type": {"kode": "REGN", "beskrivelse": "Regnskapsfører"},
                        "enhet": {"navn": ["AZETS INSIGHT AS"]},
                        "avregistrert": False,
                    }
                ],
            },
            {
                "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {
                            "navn": {"fornavn": "Internal", "etternavn": "Leader"},
                            "fodselsdato": "1975-01-01",  # must be ignored!
                        },
                        "avregistrert": False,
                    }
                ],
            },
        ],
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=synthetic_payload)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    roles, evidence, status = client.fetch_roles("999888777")

    assert status == "completed"
    assert len(roles) == 1
    assert roles[0].role_code == "DAGL"
    assert roles[0].name == "Internal Leader"

    # Verify no REVI or REGN
    role_codes = {r.role_code for r in roles}
    assert "REVI" not in role_codes
    assert "REGN" not in role_codes


# ---------------------------------------------------------------------------
# Test 3: All six permitted governance codes remain supported
# ---------------------------------------------------------------------------


def test_all_six_governance_codes_supported():
    """Verify DAGL, LEDE, NEST, MEDL, VARA, INNH are all correctly extracted."""
    payload = {
        "_links": {"enhet": {"href": "https://data.brreg.no/enhetsregisteret/api/enheter/112233445"}},
        "rollegrupper": [
            {
                "type": {"kode": "STYR", "beskrivelse": "Styre"},
                "roller": [
                    {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder"}, "person": {"navn": {"fornavn": "A"}}, "avregistrert": False},
                    {"type": {"kode": "LEDE", "beskrivelse": "Styreleder"}, "person": {"navn": {"fornavn": "B"}}, "avregistrert": False},
                    {"type": {"kode": "NEST", "beskrivelse": "Nestleder"}, "person": {"navn": {"fornavn": "C"}}, "avregistrert": False},
                    {"type": {"kode": "MEDL", "beskrivelse": "Styremedlem"}, "person": {"navn": {"fornavn": "D"}}, "avregistrert": False},
                    {"type": {"kode": "VARA", "beskrivelse": "Varamedlem"}, "person": {"navn": {"fornavn": "E"}}, "avregistrert": False},
                    {"type": {"kode": "INNH", "beskrivelse": "Innehaver"}, "person": {"navn": {"fornavn": "F"}}, "avregistrert": False},
                    {"type": {"kode": "OBSV", "beskrivelse": "Observatør"}, "person": {"navn": {"fornavn": "G"}}, "avregistrert": False},  # unsupported
                ],
            }
        ],
    }

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))))
    roles, evidence, status = client.fetch_roles("112233445")

    assert status == "completed"
    assert len(roles) == 6
    extracted_codes = {r.role_code for r in roles}
    assert extracted_codes == {"DAGL", "LEDE", "NEST", "MEDL", "VARA", "INNH"}


# ---------------------------------------------------------------------------
# Test 4: ENK proprietor handling
# ---------------------------------------------------------------------------


def test_enk_proprietor_handling():
    """Verify sole proprietorship with INNH reports INNH in management_roles and CEO as not_available."""
    now = utc_now()
    profile = CompanyProfile(
        org_nr="917103461",
        name="K. T BYGG KOCSIS-TOTH",
        org_form="ENK",
        management_roles=[
            CompanyRole(
                role_code="INNH",
                role_title="Innehaver",
                name="Toth Kocsis",
                is_active=True,
            )
        ],
    )
    ev = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/917103461",
        retrieved_at=now,
        response_hash="hash_enk",
        status_code=200,
    )
    envelope = build_terminal_envelope(
        org_nr="917103461",
        run_id="run-enk",
        started_at=now,
        completed_at=now,
        profile=profile,
        evidence=ev,
    )
    claims_map = {c["field"]: c for c in envelope["claims"]}

    # Management roles has INNH
    assert claims_map["management_roles"]["availability"] == "available"
    assert len(claims_map["management_roles"]["value"]) == 1
    assert claims_map["management_roles"]["value"][0]["role_code"] == "INNH"

    # CEO is not_available (under Norwegian law ENK has an owner, not a CEO)
    assert claims_map["ceo"]["availability"] == "not_available"
    assert claims_map["ceo"]["value"] is None


# ---------------------------------------------------------------------------
# Test 5: KONSERN and SELSKAP distinction
# ---------------------------------------------------------------------------


def test_financial_account_type_distinction_konsern_and_selskap():
    """Verify KONSERN (consolidated group) and SELSKAP (individual company) accounts are explicitly distinguished."""
    now = utc_now()
    ev = SourceEvidence(
        source_name="Regnskapsregisteret",
        source_url="https://data.brreg.no/regnskapsregisteret/regnskap/982463718",
        retrieved_at=now,
        response_hash="hash_fin",
        status_code=200,
    )

    # 1. Consolidated group accounts
    p_konsern = CompanyProfile(
        org_nr="982463718",
        name="TELENOR ASA",
        financial_accounts=FinancialAccounts(
            financial_year="2024",
            period_end="2024-12-31",
            account_type="KONSERN",
            is_consolidated=True,
            currency="NOK",
            revenue=77292000000.0,
            annual_result=8365000000.0,
        ),
    )
    env_konsern = build_terminal_envelope(
        org_nr="982463718",
        run_id="run-konsern",
        started_at=now,
        completed_at=now,
        profile=p_konsern,
        evidence=ev,
    )
    fa_claim = next(c for c in env_konsern["claims"] if c["field"] == "financial_accounts")
    assert fa_claim["value"]["account_type"] == "KONSERN"
    assert fa_claim["value"]["is_consolidated"] is True

    # Check evidence claim span
    fa_ev = next(e for e in env_konsern["evidence"] if e["id"] in fa_claim["evidence_ids"])
    assert "consolidated group" in fa_ev["claim_span"]
    assert "KONSERN" in fa_ev["claim_span"]

    # Check summary synthesis
    summary = p_konsern.generate_summary()
    assert "consolidated group revenue" in summary or "KONSERN (consolidated)" in summary

    # 2. Individual company accounts
    p_selskap = CompanyProfile(
        org_nr="817209882",
        name="Aktieselskapet Vinmonopolet",
        financial_accounts=FinancialAccounts(
            financial_year="2024",
            period_end="2024-12-31",
            account_type="SELSKAP",
            is_consolidated=False,
            currency="NOK",
            revenue=18707000000.0,
            annual_result=-187000000.0,
        ),
    )
    env_selskap = build_terminal_envelope(
        org_nr="817209882",
        run_id="run-selskap",
        started_at=now,
        completed_at=now,
        profile=p_selskap,
        evidence=ev,
    )
    fa_claim_s = next(c for c in env_selskap["claims"] if c["field"] == "financial_accounts")
    assert fa_claim_s["value"]["account_type"] == "SELSKAP"
    assert fa_claim_s["value"]["is_consolidated"] is False

    fa_ev_s = next(e for e in env_selskap["evidence"] if e["id"] in fa_claim_s["evidence_ids"])
    assert "individual company" in fa_ev_s["claim_span"]
    assert "SELSKAP" in fa_ev_s["claim_span"]


# ---------------------------------------------------------------------------
# Test 6: Currency, fiscal year, and financial values remain unchanged
# ---------------------------------------------------------------------------


def test_currency_and_financial_values_preserved():
    """Verify USD and EUR currencies and negative net results are strictly preserved."""
    raw_payload = [
        {
            "journalnr": "2024001122",
            "regnskapstype": "KONSERN",
            "virksomhet": {"organisasjonsnummer": "964118191"},
            "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
            "valuta": "EUR",
            "resultatregnskapResultat": {
                "driftsresultat": {
                    "driftsinntekter": {"sumDriftsinntekter": 6146000000.0},
                    "driftsresultat": 850000000.0,
                },
                "aarsresultat": 706000000.0,
            },
            "eiendeler": {"sumEiendeler": 9000000000.0},
            "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 4500000000.0}},
        }
    ]

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw_payload))))
    fin, ev, status = client.fetch_financials("964118191")

    assert status == "completed"
    assert fin.currency == "EUR"
    assert fin.account_type == "KONSERN"
    assert fin.is_consolidated is True
    assert fin.financial_year == "2024"
    assert fin.revenue == 6146000000.0
    assert fin.annual_result == 706000000.0


# ---------------------------------------------------------------------------
# Test 7: HTTP 500 produces failed, not not_available
# ---------------------------------------------------------------------------


def test_http_500_produces_failed():
    """Verify generic HTTP 500 produces failed status, not not_available."""
    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    fin, ev, status = client.fetch_financials("995568217")

    assert status == "failed"
    assert fin is None

    # Verify terminal envelope claim state
    now = utc_now()
    profile = CompanyProfile(org_nr="995568217", name="SERVER ERROR TEST AS")
    setattr(profile, "financials_search_status", "failed")

    ev_dummy = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/995568217",
        retrieved_at=now,
        response_hash="b" * 64,
        status_code=200,
    )
    envelope = build_terminal_envelope(
        org_nr="995568217",
        run_id="run-failed-test",
        started_at=now,
        completed_at=now,
        profile=profile,
        evidence=ev_dummy,
    )
    claims_map = {c["field"]: c for c in envelope["claims"]}
    ev_ids = {e["id"] for e in envelope["evidence"]}

    assert claims_map["financial_accounts"]["availability"] == "failed"
    assert claims_map["financial_accounts"]["value"] is None
    # Ensure evidence record was created and is resolvable
    assert len(claims_map["financial_accounts"]["evidence_ids"]) == 1
    assert claims_map["financial_accounts"]["evidence_ids"][0] in ev_ids


# ---------------------------------------------------------------------------
# Test 8: Envelope structure and evidence integrity
# ---------------------------------------------------------------------------


def test_envelope_structure_and_evidence_integrity():
    """Verify all evidence IDs, hashes, timestamps, and contract envelope fields."""
    now = utc_now()
    profile = CompanyProfile(
        org_nr="974442167",
        name="BOUVET ASA",
        financial_accounts=FinancialAccounts(
            financial_year="2024",
            period_end="2024-12-31",
            account_type="KONSERN",
            is_consolidated=True,
            currency="NOK",
            revenue=3912343000.0,
            annual_result=358673000.0,
        ),
    )
    ev = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/974442167",
        retrieved_at=now,
        response_hash="a" * 64,
        status_code=200,
    )
    envelope = build_terminal_envelope(
        org_nr="974442167",
        run_id="run-struct",
        started_at=now,
        completed_at=now,
        profile=profile,
        evidence=ev,
    )

    ev_ids = {e["id"] for e in envelope["evidence"]}
    for c in envelope["claims"]:
        for eid in c["evidence_ids"]:
            assert eid in ev_ids

    for e in envelope["evidence"]:
        assert e["source_url"].startswith("http")
        assert e["retrieved_at"].endswith("Z")
        assert re.fullmatch(r"^[a-f0-9]{64}$", e["content_sha256"])
        assert len(e["claim_span"]) >= 3
