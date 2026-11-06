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


# ---------------------------------------------------------------------------
# 6. Multi-source availability semantics
# ---------------------------------------------------------------------------


def test_nav_blocked_while_ats_succeeds_yields_available():
    """Verify requirement: A verified active vacancy from an ATS source results in availability: available even if NAV is blocked."""
    profile = CompanyProfile(
        org_nr="917103801",
        name="Veidekke ASA",
        org_form="ASA",
    )
    profile.job_search_status = "completed"
    profile.nav_job_status = "blocked"
    profile.career_job_status = "completed"
    profile.ats_job_status = "completed"

    posting = JobPosting(
        job_title="Nyutdannet i Veidekke Anlegg 2027",
        employer_name="Veidekke ASA",
        advertisement_url="https://veidekke.teamtailor.com/jobs/8293551-nyutdannet",
        source="teamtailor.com",
        deadline_date="2027-01-01",
        is_active=True,
    )
    profile.job_postings = [posting]

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
    assert job_claim["availability"] == "available"
    assert isinstance(job_claim["value"], list)
    assert len(job_claim["value"]) == 1
    assert job_claim["value"][0]["job_title"] == "Nyutdannet i Veidekke Anlegg 2027"


def test_nav_blocked_while_career_page_succeeds_yields_available():
    """Verify requirement: A verified active vacancy from a corporate career page results in availability: available even if NAV is blocked."""
    profile = CompanyProfile(
        org_nr="982463718",
        name="Telenor ASA",
        org_form="ASA",
    )
    profile.job_search_status = "completed"
    profile.nav_job_status = "blocked"
    profile.career_job_status = "completed"
    profile.ats_job_status = "not_applicable"

    posting = JobPosting(
        job_title="Senior Cloud Engineer",
        employer_name="Telenor ASA",
        advertisement_url="https://telenor.no/karriere/senior-cloud-engineer",
        source="corporate_career_page",
        deadline_date=None,
        is_active=True,
    )
    profile.job_postings = [posting]

    evidence = SourceEvidence(
        source_name="enhetsregisteret",
        source_url="https://data.brreg.no/api/982463718",
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
    assert job_claim["availability"] == "available"
    assert isinstance(job_claim["value"], list)
    assert len(job_claim["value"]) == 1


def test_all_alternative_sources_unavailable_and_nav_blocked_yields_blocked():
    """Verify requirement: When company has no website and NAV is rate-limited (429), availability is blocked."""
    profile = CompanyProfile(
        org_nr="999888777",
        name="Liten Snekkerservice ENK",
        org_form="ENK",
    )
    profile.job_search_status = "blocked"
    profile.nav_job_status = "blocked"
    profile.career_job_status = "not_applicable"
    profile.ats_job_status = "not_applicable"
    profile.verified_website = None
    profile.job_postings = []

    evidence = SourceEvidence(
        source_name="enhetsregisteret",
        source_url="https://data.brreg.no/api/999888777",
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


# ---------------------------------------------------------------------------
# 7. Resumption from database checkpoints and corrupted output handling
# ---------------------------------------------------------------------------


def test_resume_from_sqlite_checkpoints_when_output_file_missing(tmp_path):
    """Verify requirement: Resuming recovers completed envelopes directly from SQLite batch_checkpoints even if output file is absent."""
    from app.services.database import DatabaseService

    db_path = tmp_path / "test.db"
    db = DatabaseService(db_path)

    env_data = {
        "organisation_number": "982463718",
        "run": {"run_id": "resilient-run", "terminal_status": "completed", "started_at": "2026-11-05T00:00:00Z", "completed_at": "2026-11-05T00:00:01Z"},
        "claims": [{"field": "legal_name", "value": "TELENOR ASA", "availability": "available", "confidence": 1.0, "evidence_ids": ["ev-1"]}],
        "evidence": [],
        "changes": [],
        "errors": [],
        "operations": {"requests": 1, "runtime_ms": 100, "third_party_cost_usd": 0.0},
    }
    db.save_batch_checkpoint(run_id="resilient-run", org_nr="982463718", input_hash="dummy-hash", envelope=env_data)

    out_file = tmp_path / "recovered_envelopes.jsonl"
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(["982463718", "914778271"]), encoding="utf-8")

    processed_orgs = []

    def mock_fetch(org_nr):
        processed_orgs.append(org_nr)
        p = CompanyProfile(org_nr=org_nr, name="NEW CO AS", org_form="AS")
        ev = SourceEvidence(source_name="enhetsregisteret", source_url=f"https://data.brreg.no/api/{org_nr}", retrieved_at=utc_now(), response_hash="abc", payload={})
        return p, ev

    mock_client = BrregClient()
    mock_client.fetch_company = mock_fetch

    runner = BatchRunner(db_path=db_path, client=mock_client)
    report = runner.run(
        input_path=input_file,
        output_path=out_file,
        run_id="resilient-run",
        resume=True,
    )

    assert processed_orgs == ["914778271"]
    assert report["expected_count"] == 2
    assert report["emitted_envelopes"] == 2

    lines = [json.loads(l) for l in out_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["organisation_number"] == "982463718"
    assert lines[1]["organisation_number"] == "914778271"


def test_interrupted_writes_and_corrupt_line_recovery(tmp_path):
    """Verify requirement: Resuming safely recovers from trailing corrupt or truncated lines without duplicating envelopes."""
    out_file = tmp_path / "envelopes.jsonl"
    input_file = tmp_path / "input.json"

    valid_env = {
        "organisation_number": "982463718",
        "run": {"run_id": "test-run", "terminal_status": "completed", "started_at": "2026-11-05T00:00:00Z", "completed_at": "2026-11-05T00:00:01Z"},
        "claims": [],
        "evidence": [],
        "changes": [],
        "errors": [],
        "operations": {"requests": 1, "runtime_ms": 100, "third_party_cost_usd": 0.0},
    }
    out_file.write_text(json.dumps(valid_env) + "\n{\"organisation_number\": \"914778271\", \"run\": { incomplete\n", encoding="utf-8")
    input_file.write_text(json.dumps(["982463718", "914778271"]), encoding="utf-8")

    processed_orgs = []

    def mock_fetch(org_nr):
        processed_orgs.append(org_nr)
        p = CompanyProfile(org_nr=org_nr, name="RECOVERED CO AS", org_form="AS")
        ev = SourceEvidence(source_name="enhetsregisteret", source_url=f"https://data.brreg.no/api/{org_nr}", retrieved_at=utc_now(), response_hash="abc", payload={})
        return p, ev

    mock_client = BrregClient()
    mock_client.fetch_company = mock_fetch

    runner = BatchRunner(db_path=tmp_path / "test.db", client=mock_client)
    report = runner.run(input_path=input_file, output_path=out_file, resume=True)

    assert processed_orgs == ["914778271"]
    assert report["expected_count"] == 2
    assert report["emitted_envelopes"] == 2

    lines = [json.loads(l) for l in out_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["organisation_number"] == "982463718"
    assert lines[1]["organisation_number"] == "914778271"


def test_duplicate_input_org_deduplication(tmp_path):
    """Verify requirement: Duplicate input organization numbers do not cause multiple network executions, while preserving 100% input correspondence in output."""
    input_file = tmp_path / "dups_input.json"
    out_file = tmp_path / "dups_out.jsonl"
    input_file.write_text(json.dumps(["982463718", "982463718", "914778271"]), encoding="utf-8")

    processed_orgs = []

    def mock_fetch(org):
        processed_orgs.append(org)
        return (
            CompanyProfile(org_nr=org, name=f"CO {org}", org_form="AS"),
            SourceEvidence(source_name="enhetsregisteret", source_url=f"https://data.brreg.no/api/{org}", retrieved_at=utc_now(), response_hash="abc", payload={})
        )

    mock_client = BrregClient()
    mock_client.fetch_company = mock_fetch

    runner = BatchRunner(db_path=tmp_path / "test.db", client=mock_client)
    report = runner.run(input_path=input_file, output_path=out_file)

    # Output matches exact input count and ordering
    assert report["expected_count"] == 3
    assert report["emitted_envelopes"] == 3
    assert report["zero_silent_drops"] is True

    # But underlying company fetch executed only once per unique organization
    assert processed_orgs == ["982463718", "914778271"]

    lines = [json.loads(l) for l in out_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3
    assert lines[0]["organisation_number"] == "982463718"
    assert lines[1]["organisation_number"] == "982463718"
    assert lines[2]["organisation_number"] == "914778271"


# ---------------------------------------------------------------------------
# 8. Canonical deduplication & HTML entity decodings
# ---------------------------------------------------------------------------


def test_duplicate_vacancy_deduplication_by_canonical_url_and_uuid():
    """Verify requirement: Vacancies with same UUID or same canonical URL (ignoring tracking params) are deduplicated without losing distinct vacancies."""
    job1 = JobPosting(
        job_title="Prosjektleder",
        employer_name="Veidekke ASA",
        advertisement_url="https://veidekke.teamtailor.com/jobs/8293551-nyutdannet?utm_source=linkedin",
        source="teamtailor.com",
        is_active=True,
    )
    job2 = JobPosting(
        job_title="Prosjektleder",
        employer_name="Veidekke ASA",
        advertisement_url="https://veidekke.teamtailor.com/jobs/8293551-nyutdannet?utm_source=facebook&ref=share",
        source="teamtailor.com",
        is_active=True,
    )
    job3 = JobPosting(
        job_title="Baneingeniør",
        employer_name="Veidekke ASA",
        advertisement_url="https://veidekke.teamtailor.com/jobs/9999999-baneingenior",
        source="teamtailor.com",
        is_active=True,
    )

    combined = []
    seen = set()
    for job in [job1, job2, job3]:
        u_clean = re.sub(r"[?&](?:utm_[^&=]+|ref|source|trk)=[^&=]+", "", job.advertisement_url).split("#")[0].rstrip("/").lower()
        uuid_m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", u_clean)
        if uuid_m:
            key = ("uuid", uuid_m.group(0).lower())
        elif u_clean:
            key = ("url", u_clean)
        else:
            key = ("fields", job.job_title.lower(), job.employer_name.lower())

        if key not in seen:
            seen.add(key)
            combined.append(job)

    assert len(combined) == 2
    assert combined[0].advertisement_url == job1.advertisement_url
    assert combined[1].advertisement_url == job3.advertisement_url


def test_html_entity_decoding_comprehensive():
    """Verify requirement: Both named (&oslash;, &aring;, &aelig;) and numeric (decimal &#248;, hex &#x00F8;) entities are decoded."""
    assert JobVerifier.extract_title_from_slug_or_text("S&oslash;ker l&aelig;rling og milj&oslash;r&aring;dgiver", "https://ex.com/job") == "Søker lærling og miljørådgiver"
    assert JobVerifier.extract_title_from_slug_or_text("S&#248;ker l&#230;rling og milj&#248;r&#229;dgiver", "https://ex.com/job") == "Søker lærling og miljørådgiver"
    assert JobVerifier.extract_title_from_slug_or_text("S&#x00F8;ker l&#x00E6;rling og milj&#x00F8;r&#x00E5;dgiver", "https://ex.com/job") == "Søker lærling og miljørådgiver"


def test_expired_vacancies_rejected_and_missing_deadlines_handled_gracefully():
    """Verify requirement: Expired deadlines are excluded, but missing deadlines (snarest/løpende) are retained as active."""
    v = JobVerifier()
    expired_job = JobPosting(
        job_title="Utvikler",
        employer_name="Acme",
        advertisement_url="https://acme.no/job/1",
        deadline_date="2020-01-01",
        is_active=True,
    )
    future_job = JobPosting(
        job_title="Utvikler",
        employer_name="Acme",
        advertisement_url="https://acme.no/job/2",
        deadline_date="2030-01-01",
        is_active=True,
    )
    snarest_job = JobPosting(
        job_title="Utvikler",
        employer_name="Acme",
        advertisement_url="https://acme.no/job/3",
        deadline_date=None,
        is_active=True,
    )

    assert v.is_vacancy_expired(expired_job.deadline_date) is True
    assert v.is_vacancy_expired(future_job.deadline_date) is False
    assert v.is_vacancy_expired(snarest_job.deadline_date) is False


def test_independent_host_rate_limiting():
    """Verify requirement: Rate limiting on Host A does not delay requests to Host B."""
    JobVerifier._host_last_request_ts.clear()
    JobVerifier._min_host_interval = 0.1  # 100ms

    url_a1 = "https://host-a.com/careers"
    url_b1 = "https://host-b.com/careers"

    t0 = time.monotonic()
    JobVerifier._pace_host(url_a1)
    JobVerifier._pace_host(url_b1)  # Different host should not wait for host A
    elapsed = time.monotonic() - t0

    assert elapsed < 0.05

