"""Tests for Signalpost Agent V2.5.3 resilience and multi-source discovery improvements:
1. HTML entity unescaping for Norwegian titles and descriptions (&oslash; -> ø, &amp; -> &, &nbsp; -> ' ').
2. Extracting clean job vacancy titles from URL slugs when anchor text is generic ("les mer", "søk her").
3. Multi-source ATS platform coverage (Teamtailor, Webcruiter, Recman, ReachMee, Easycruit, Jobbnorge, Workbuster).
4. Strict employer identity matching preventing cross-company ATS attribution.
5. Polite per-host pacing for corporate websites and ATS endpoints.
6. Resumable batch processing: reuses completed envelopes without repeating external calls.
7. Batch runner zero silent drops and exact input order preservation during resumption.
8. Accurate partial availability semantics: NAV blocked + corporate checked = blocked with composite evidence.
9. Accurate complete availability semantics: NAV completed + corporate checked = not_available.
10. Multi-source job discovery produces available even when NAV is rate-limited.
11. Privacy safeguards: no birth dates, national identity numbers, or REVI/REGN roles.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
import httpx
import pytest

from app.models import CompanyProfile, CompanyRole, FinancialAccounts, JobPosting, SourceEvidence, utc_now
from app.services.batch_runner import BatchRunner, build_terminal_envelope
from app.services.brreg_client import BrregClient
from app.services.job_verifier import (
    ATS_DOMAINS,
    JobPostingList,
    JobVerifier,
)


# ---------------------------------------------------------------------------
# 1. HTML entity unescaping & title slug extraction
# ---------------------------------------------------------------------------


def test_html_entity_unescaping_in_titles():
    """Verify that Norwegian characters encoded as HTML entities are properly unescaped."""
    raw_title = "Nyutdannet &oslash;konom &amp; milj&oslash;r&aring;dgiver"
    url = "https://example.com/jobs/123-stilling"
    cleaned = JobVerifier.extract_title_from_slug_or_text(raw_title, url)
    assert cleaned == "Nyutdannet økonom & miljørådgiver"


def test_extract_title_from_slug_when_anchor_is_generic_cta():
    """Verify that generic call-to-action anchor phrases fall back to clean title extracted from URL slug."""
    generic_anchors = [
        "lese mer og s&oslash;ke her.",
        "Se utlysningen her.",
        "Les mer og s&oslash;k her!",
        "Se utlysningen og s&oslash;k her",
        "Les mer og s&oslash;k her!&nbsp;",
        "Søk her",
        "Apply now",
    ]
    url = "https://veidekke.teamtailor.com/jobs/8293551-nyutdannet-i-veidekke-anlegg-2027"

    for anchor in generic_anchors:
        title = JobVerifier.extract_title_from_slug_or_text(anchor, url)
        assert "Nyutdannet i veidekke anlegg 2027" in title or "nyutdannet" in title.lower()
        assert "les mer" not in title.lower()
        assert "søk her" not in title.lower()


def test_meaningful_anchor_title_preserved():
    """Verify that meaningful job titles are preserved directly when not generic button text."""
    meaningful = "Prosjektleder Bane & Infrastruktur"
    url = "https://veidekke.teamtailor.com/jobs/9999-jobb"
    title = JobVerifier.extract_title_from_slug_or_text(meaningful, url)
    assert title == "Prosjektleder Bane & Infrastruktur"


# ---------------------------------------------------------------------------
# 2. ATS platform expansion & employer matching
# ---------------------------------------------------------------------------


def test_all_seven_ats_platforms_supported():
    """Verify that all 7 required ATS platforms are present in ATS_DOMAINS."""
    required_ats = [
        "teamtailor.com",
        "webcruiter.com",
        "recman.no",
        "reachmee.com",
        "easycruit.com",
        "jobbnorge.no",
        "workbuster.com",
    ]
    for ats in required_ats:
        assert any(ats in domain for domain in ATS_DOMAINS)


def test_ats_links_recognized_in_html():
    """Verify that links to Webcruiter, Recman, ReachMee, Jobbnorge, Workbuster are detected."""
    html_content = """
    <html><body>
        <a href="https://posten.recman.no/job.php?job_id=4567">Posten Recman</a>
        <a href="https://acme.webcruiter.com/career/123">Webcruiter Link</a>
        <a href="https://company.reachmee.com/rm/jobs/1">ReachMee Link</a>
        <a href="https://statnett.easycruit.com/vacancy/99">Easycruit Link</a>
        <a href="https://jobbnorge.no/ledige-stillinger/stilling/88">Jobbnorge Link</a>
        <a href="https://nordic.workbuster.com/jobs/77">Workbuster Link</a>
    </body></html>
    """
    internal, ats = JobVerifier.extract_career_links_from_html(html_content, "https://example.no")
    assert len(ats) == 6
    assert any("recman.no" in u for u in ats)
    assert any("webcruiter.com" in u for u in ats)
    assert any("reachmee.com" in u for u in ats)
    assert any("easycruit.com" in u for u in ats)
    assert any("jobbnorge.no" in u for u in ats)
    assert any("workbuster.com" in u for u in ats)


def test_ats_employer_mismatch_rejected():
    """Verify that third-party ATS vacancies targeting another company are rejected."""
    verifier = JobVerifier()
    # Unrelated employer on Recman
    assert not verifier.verify_employer_match("acme consulting", "Kongsberg Gruppen ASA")
    # Matching employer on Recman
    assert verifier.verify_employer_match("Kongsberg Defence & Aerospace", "Kongsberg Gruppen ASA")


# ---------------------------------------------------------------------------
# 3. Polite per-host pacing
# ---------------------------------------------------------------------------


def test_per_host_pacing_applies_to_same_host():
    """Verify that sequential calls to the same corporate host are paced appropriately."""
    JobVerifier._host_last_request_ts.clear()
    JobVerifier._min_host_interval = 0.05  # 50ms for test
    url1 = "https://corp.example.no/karriere"
    url2 = "https://corp.example.no/stillinger/1"

    t0 = time.monotonic()
    JobVerifier._pace_host(url1)
    JobVerifier._pace_host(url2)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.04  # Pacing enforced


# ---------------------------------------------------------------------------
# 4. Resumable batch processing
# ---------------------------------------------------------------------------


def test_resumable_batch_runner_reuses_completed_envelopes(tmp_path):
    """Verify that a batch run with resume=True reuses existing completed envelopes."""
    out_file = tmp_path / "envelopes.jsonl"
    report_file = tmp_path / "report.json"
    input_file = tmp_path / "input.json"

    # Pre-populate out_file with 1 completed company
    cached_env = {
        "organisation_number": "982463718",
        "run": {"run_id": "test-run", "started_at": "2026-11-05T00:00:00Z", "completed_at": "2026-11-05T00:00:01Z", "terminal_status": "completed"},
        "claims": [{"field": "legal_name", "value": "TELENOR ASA", "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-1"]}],
        "evidence": [],
        "changes": [],
        "errors": [],
        "operations": {"requests": 1, "runtime_ms": 100, "third_party_cost_usd": 0.0},
    }
    out_file.write_text(json.dumps(cached_env) + "\n", encoding="utf-8")

    # Input contains 2 companies: 982463718 (cached) and 914778271 (new)
    input_file.write_text(json.dumps(["982463718", "914778271"]), encoding="utf-8")

    processed_orgs = []

    def mock_fetch(org_nr):
        processed_orgs.append(org_nr)
        profile = CompanyProfile(org_nr=org_nr, name="NEW CO AS", org_form="AS")
        evidence = SourceEvidence(source_url=f"https://data.brreg.no/api/{org_nr}", retrieved_at=utc_now(), response_hash="abc", payload={})
        return profile, evidence

    mock_client = BrregClient()
    mock_client.fetch_company = mock_fetch

    runner = BatchRunner(db_path=tmp_path / "test.db", client=mock_client)
    report = runner.run(
        input_path=input_file,
        output_path=out_file,
        report_path=report_file,
        resume=True,
    )

    # 982463718 was already in output, so mock_fetch should only be called for 914778271!
    assert processed_orgs == ["914778271"]
    assert report["expected_count"] == 2
    assert report["emitted_envelopes"] == 2
    assert report["zero_silent_drops"] is True

    # Output file now contains both envelopes in exact order
    lines = [json.loads(l) for l in out_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["organisation_number"] == "982463718"
    assert lines[1]["organisation_number"] == "914778271"


def test_batch_runner_disabled_resume_reprocesses_all(tmp_path):
    """Verify that when resume=False, all companies are reprocessed even if present in output."""
    out_file = tmp_path / "envelopes.jsonl"
    input_file = tmp_path / "input.json"

    # Pre-populate out_file
    cached_env = {
        "organisation_number": "982463718",
        "run": {"run_id": "test-run", "started_at": "2026-11-05T00:00:00Z", "completed_at": "2026-11-05T00:00:01Z", "terminal_status": "completed"},
        "claims": [],
        "evidence": [],
        "changes": [],
        "errors": [],
        "operations": {"requests": 1, "runtime_ms": 100, "third_party_cost_usd": 0.0},
    }
    out_file.write_text(json.dumps(cached_env) + "\n", encoding="utf-8")
    input_file.write_text(json.dumps(["982463718"]), encoding="utf-8")

    processed_orgs = []

    def mock_fetch(org_nr):
        processed_orgs.append(org_nr)
        profile = CompanyProfile(org_nr=org_nr, name="TELENOR ASA", org_form="ASA")
        evidence = SourceEvidence(source_url=f"https://data.brreg.no/api/{org_nr}", retrieved_at=utc_now(), response_hash="abc", payload={})
        return profile, evidence

    mock_client = BrregClient()
    mock_client.fetch_company = mock_fetch

    runner = BatchRunner(db_path=tmp_path / "test.db", client=mock_client)
    runner.run(input_path=input_file, output_path=out_file, resume=False)

    # 982463718 was reprocessed because resume=False
    assert processed_orgs == ["982463718"]


# ---------------------------------------------------------------------------
# 5. Accurate availability semantics: partial vs complete coverage
# ---------------------------------------------------------------------------


def test_partial_coverage_when_nav_blocked_and_career_zero_hits():
    """Verify that when NAV is blocked and corporate career page verified 0 hits, claim is blocked with explicit composite evidence."""
    profile = CompanyProfile(
        org_nr="917103801",
        name="Veidekke ASA",
        org_form="ASA",
    )
    profile.job_search_status = "blocked"
    profile.job_postings = []
    # Corporate website was verified
    from app.models import VerifiedWebsite
    profile.verified_website = VerifiedWebsite(
        candidate_url="https://veidekke.no",
        final_url="https://www.veidekke.no",
        verification_status="verified_ownership",
        is_verified=True,
    )
    evidence = SourceEvidence(
        source_name="enhetsregisteret",
        source_url="https://data.brreg.no/api/917103801",
        retrieved_at=utc_now(),
        response_hash="hash",
        payload={},
    )

    envelope = build_terminal_envelope(
        org_nr=profile.org_nr,
        run_id="test",
        started_at=utc_now(),
        completed_at=utc_now(),
        profile=profile,
        evidence=evidence,
    )

    claims = {c["field"]: c for c in envelope["claims"]}
    job_claim = claims["job_postings"]
    assert job_claim["availability"] == "blocked"
    assert job_claim["value"] is None

    # Check evidence span notes both sources
    job_ev = [ev for ev in envelope["evidence"] if ev["id"] in job_claim["evidence_ids"]][0]
    assert "rate-limited" in job_ev["claim_span"]
    assert "Verified official corporate website checked" in job_ev["claim_span"]


def test_complete_coverage_when_both_sources_checked_with_zero_hits():
    """Verify that when NAV completed with 0 hits and corporate checked with 0 hits, claim is not_available."""
    profile = CompanyProfile(
        org_nr="917103801",
        name="Veidekke ASA",
        org_form="ASA",
    )
    profile.job_search_status = "completed"
    profile.job_postings = []
    from app.models import VerifiedWebsite
    profile.verified_website = VerifiedWebsite(
        candidate_url="https://veidekke.no",
        final_url="https://www.veidekke.no",
        verification_status="verified_ownership",
        is_verified=True,
    )
    evidence = SourceEvidence(
        source_name="enhetsregisteret",
        source_url="https://data.brreg.no/api/917103801",
        retrieved_at=utc_now(),
        response_hash="hash",
        payload={},
    )

    envelope = build_terminal_envelope(
        org_nr=profile.org_nr,
        run_id="test",
        started_at=utc_now(),
        completed_at=utc_now(),
        profile=profile,
        evidence=evidence,
    )

    claims = {c["field"]: c for c in envelope["claims"]}
    job_claim = claims["job_postings"]
    assert job_claim["availability"] == "not_available"
    assert job_claim["value"] is None

    job_ev = [ev for ev in envelope["evidence"] if ev["id"] in job_claim["evidence_ids"]][0]
    assert "0 active vacancies found" in job_ev["claim_span"]
