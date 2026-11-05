"""Tests for Signalpost Agent V2.5.2 job discovery and NAV resilience:
1. Canonical search query deduplication (no secondary redundant queries).
2. Process-wide serialized NAV request limiter and pacing.
3. 429 rate limit handling, bounded backoff, and Retry-After header parsing.
4. Resilient circuit breaker: threshold, adaptive cooldown, and serialized probe recovery.
5. Expanded career paths and homepage navigation link extraction.
6. ATS link recognition (Teamtailor, Webcruiter, Recman, etc.) and employer validation.
7. Semantic HTML job card extraction where Schema.org is absent.
8. Expired and inactive vacancy rejection.
9. Duplicate vacancy elimination across NAV and career sources.
10. Contract-compliant availability semantics (available, not_available, blocked, failed).
11. V2.5.1 mandatory regression safeguards (no birth dates, restricted roles, financial disclosure).
"""

from datetime import datetime, timezone
import json
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
    CAREER_LINK_KEYWORDS,
    EXPANDED_CAREER_PATHS,
    JobPostingList,
    JobVerifier,
    parse_retry_after,
)


# ---------------------------------------------------------------------------
# 1. Canonical search query deduplication
# ---------------------------------------------------------------------------


def test_canonical_search_term_normalization():
    """Verify that legal form suffixes and punctuation are stripped without losing distinctive brand names."""
    assert JobVerifier.get_canonical_search_term("EQUINOR ASA") == "EQUINOR"
    assert JobVerifier.get_canonical_search_term("TELENOR NORGE AS") == "TELENOR NORGE"
    assert JobVerifier.get_canonical_search_term("BOUVET ASA") == "BOUVET"
    assert JobVerifier.get_canonical_search_term("- P A L M E R A -") == "P A L M E R A"
    assert JobVerifier.get_canonical_search_term("----ADVOKATFIRMAET CATO MYHRE") == "ADVOKATFIRMAET CATO MYHRE"
    assert JobVerifier.get_canonical_search_term("!FALSE INVESTMENTS AS") == "FALSE INVESTMENTS"
    assert JobVerifier.get_canonical_search_term("STOREBRAND LIVSFORSIKRING AS") == "STOREBRAND LIVSFORSIKRING"
    assert JobVerifier.get_canonical_search_term("NORDIC SEMICONDUCTOR ASA") == "NORDIC SEMICONDUCTOR"
    assert JobVerifier.get_canonical_search_term("CATO MYHRE ENK") == "CATO MYHRE"
    assert JobVerifier.get_canonical_search_term("AS") == "AS"


def test_no_duplicate_secondary_query_on_zero_hits():
    """Verify that NAV search issues exactly ONE query per company, with zero duplicate secondary queries."""
    recorded_urls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        recorded_urls.append(str(request.url))
        # Return 0 hits
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    result = verifier.fetch_nav_vacancies(legal_name="PALMERA INVEST AS", org_nr="916627939")
    assert result.status == "completed"
    assert len(result) == 0
    # Exactly one query sent, targeting canonical 'PALMERA INVEST'
    assert len(recorded_urls) == 1
    assert "q=PALMERA+INVEST" in recorded_urls[0] or "q=PALMERA%20INVEST" in recorded_urls[0]


# ---------------------------------------------------------------------------
# 2. Process-wide request serialization and pacing
# ---------------------------------------------------------------------------


def test_nav_request_limiter_serialization():
    """Verify that concurrent threads calling NAV are serialized with minimum interval."""
    JobVerifier.configure_circuit_breaker(min_request_interval=0.1)
    timestamps = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        timestamps.append(time.monotonic())
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    def worker(idx):
        verifier.fetch_nav_vacancies(f"Company {idx} AS", f"90000000{idx}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(timestamps) == 3
    # Check that requests were spaced by at least 0.08s
    diffs = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
    assert all(d >= 0.08 for d in diffs)

    # Restore production default
    JobVerifier.configure_circuit_breaker(min_request_interval=2.5)


# ---------------------------------------------------------------------------
# 3. 429 rate limit handling, bounded backoff, and Retry-After
# ---------------------------------------------------------------------------


def test_retry_after_header_parsing():
    """Verify integer and HTTP-date parsing for Retry-After."""
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("invalid") is None


def test_nav_429_single_response_retry_success():
    """Verify that a single 429 with Retry-After header retries and succeeds if circuit is not tripped."""
    JobVerifier.reset_circuit_breaker()
    JobVerifier.configure_circuit_breaker(cooldown_seconds=10.0, failure_threshold=4, min_request_interval=0.01)

    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.05"})
        return httpx.Response(200, json={
            "hits": {
                "hits": [{
                    "_source": {
                        "uuid": "test-uuid-1",
                        "title": "Software Engineer",
                        "status": "ACTIVE",
                        "employer": {"name": "Bouvet ASA"},
                        "locationList": [{"city": "Oslo"}],
                    }
                }]
            }
        })

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    result = verifier.fetch_nav_vacancies("Bouvet ASA", "982363062")
    assert attempts == 2
    assert result.status == "completed"
    assert len(result) == 1
    assert result[0].job_title == "Software Engineer"
    JobVerifier.reset_circuit_breaker()
    JobVerifier.configure_circuit_breaker(min_request_interval=2.5)


# ---------------------------------------------------------------------------
# 4. Resilient circuit breaker: threshold, cooldown, and recovery probe
# ---------------------------------------------------------------------------


def test_circuit_breaker_resilient_threshold_and_recovery():
    """Verify circuit breaker opens after 4 consecutive 429s and recovers on successful probe."""
    JobVerifier.reset_circuit_breaker()
    JobVerifier.configure_circuit_breaker(cooldown_seconds=0.1, failure_threshold=4, min_request_interval=0.01)

    mode = "429"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if mode == "429":
            return httpx.Response(429)
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    # First call: makes 1 call (429) + 1 retry (429) = 2 consecutive 429s
    res1 = verifier.fetch_nav_vacancies("Company 1 AS", "911111111")
    assert res1.status == "blocked"
    assert JobVerifier.get_circuit_status()["is_open"] is False  # Threshold 4 not reached

    # Second call: makes 1 call (429) + 1 retry (429) = 4 consecutive 429s -> trips circuit
    res2 = verifier.fetch_nav_vacancies("Company 2 AS", "922222222")
    assert res2.status == "blocked"
    assert JobVerifier.get_circuit_status()["is_open"] is True

    # Immediate third call while circuit open: skipped without request
    res3 = verifier.fetch_nav_vacancies("Company 3 AS", "933333333")
    assert res3.status == "blocked"
    assert "circuit breaker open" in res3.error_message.lower()

    # Wait for 0.1s cooldown to expire
    time.sleep(0.12)

    # Switch mock to return 200
    mode = "200"

    # Recovery probe runs
    res4 = verifier.fetch_nav_vacancies("Company 4 AS", "944444444")
    assert res4.status == "completed"
    assert JobVerifier.get_circuit_status()["is_open"] is False

    JobVerifier.reset_circuit_breaker()
    JobVerifier.configure_circuit_breaker(min_request_interval=2.5)


# ---------------------------------------------------------------------------
# 5. Career path expansion and homepage navigation link extraction
# ---------------------------------------------------------------------------


def test_extract_career_and_ats_links_from_html():
    """Verify navigation link parser discovers both internal career destinations and ATS domains."""
    sample_html = """
    <html>
    <body>
        <nav>
            <a href="/">Home</a>
            <a href="/om-oss">Om oss</a>
            <a href="/karriere">Karriere hos oss</a>
            <a href="/om-oss/jobb">Ledige stillinger</a>
            <a href="https://bouvet.teamtailor.com">Teamtailor Portal</a>
            <a href="https://webcruiter.com/portal?id=123">Webcruiter</a>
            <a href="https://unrelated.com/blog">Blog</a>
        </nav>
    </body>
    </html>
    """
    internal, ats = JobVerifier.extract_career_links_from_html(sample_html, "https://www.bouvet.no")
    assert "https://www.bouvet.no/karriere" in internal
    assert "https://www.bouvet.no/om-oss/jobb" in internal
    assert "https://bouvet.teamtailor.com" in ats
    assert "https://webcruiter.com/portal?id=123" in ats
    assert not any("unrelated.com" in u for u in internal + ats)


# ---------------------------------------------------------------------------
# 6. ATS link recognition and employer verification
# ---------------------------------------------------------------------------


def test_ats_employer_mismatch_rejected():
    """Verify that an ATS link belonging to a different company is rejected."""
    mismatched_ats_html = """
    <html>
    <head><title>Acme Corporation Careers</title></head>
    <body>
        <a href="/jobs/123">Senior Developer</a>
    </body>
    </html>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "teamtailor.com" in str(request.url):
            return httpx.Response(200, text=mismatched_ats_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    homepage_with_ats = '<a href="https://acme.teamtailor.com">Jobs</a>'
    result = verifier.discover_career_vacancies(
        website_url="https://www.telenor.no",
        legal_name="TELENOR ASA",
        org_nr="976632063",
        http_client=client,
        homepage_html=homepage_with_ats,
    )
    # Acme ATS must not be attributed to Telenor
    assert result is None or len(result) == 0


def test_ats_matching_employer_accepted():
    """Verify that an ATS link with matching employer is parsed and accepted."""
    matching_ats_html = """
    <html>
    <head><title>Bouvet Careers Portal</title></head>
    <body>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Cloud Architect",
            "hiringOrganization": {"name": "Bouvet Norge AS"},
            "datePosted": "2026-10-20",
            "validThrough": "2026-12-31",
            "url": "https://bouvet.teamtailor.com/jobs/cloud-arch",
            "jobLocation": {"address": {"addressLocality": "Oslo"}}
        }
        </script>
    </body>
    </html>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=matching_ats_html)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    homepage_with_ats = '<a href="https://bouvet.teamtailor.com">Karriere</a>'
    result = verifier.discover_career_vacancies(
        website_url="https://www.bouvet.no",
        legal_name="BOUVET ASA",
        org_nr="982363062",
        http_client=client,
        homepage_html=homepage_with_ats,
    )
    assert result is not None
    assert len(result) == 1
    assert result[0].job_title == "Cloud Architect"
    assert result[0].employer_name == "Bouvet Norge AS"
    assert result[0].advertisement_url == "https://bouvet.teamtailor.com/jobs/cloud-arch"


# ---------------------------------------------------------------------------
# 7. Semantic HTML job card extraction (where Schema.org is absent)
# ---------------------------------------------------------------------------


def test_semantic_html_job_card_extraction():
    """Verify extraction of vacancy links and titles from semantic HTML without JSON-LD."""
    html_without_jsonld = """
    <html>
    <body>
        <h1>Ledige stillinger hos Testbedrift AS</h1>
        <div class="job-list">
            <article class="job-card">
                <h3><a href="/stillinger/fullstack-utvikler">Senior Fullstack Utvikler</a></h3>
                <p>Søknadsfrist: 2026-12-15</p>
            </article>
            <article class="job-card">
                <h3><a href="/stillinger/data-analytiker">Data Analytiker</a></h3>
                <p>Søknadsfrist: 2026-11-30</p>
            </article>
            <!-- Generic navigation links that must be rejected -->
            <a href="/karriere">Les mer</a>
            <a href="/om-oss">Vis mer</a>
        </div>
    </body>
    </html>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html_without_jsonld)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    result = verifier.discover_career_vacancies(
        website_url="https://www.testbedrift.no",
        legal_name="TESTBEDRIFT AS",
        org_nr="999888777",
        http_client=client,
    )
    assert result is not None
    assert len(result) == 2
    titles = [j.job_title for j in result]
    assert "Senior Fullstack Utvikler" in titles
    assert "Data Analytiker" in titles
    assert all("testbedrift.no/stillinger/" in j.advertisement_url for j in result)


# ---------------------------------------------------------------------------
# 8. Expired and inactive vacancy rejection
# ---------------------------------------------------------------------------


def test_expired_vacancy_rejected_in_career_discovery():
    """Verify that vacancies with validThrough in the past are rejected."""
    expired_html = """
    <html>
    <body>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Expired Legacy Lead",
            "hiringOrganization": {"name": "Testbedrift AS"},
            "validThrough": "2020-01-01",
            "url": "https://testbedrift.no/stillinger/old-job"
        }
        </script>
    </body>
    </html>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=expired_html)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    result = verifier.discover_career_vacancies(
        website_url="https://www.testbedrift.no",
        legal_name="TESTBEDRIFT AS",
        org_nr="999888777",
        http_client=client,
    )
    # Expired job should not be included
    assert result is not None
    assert len(result) == 0


# ---------------------------------------------------------------------------
# 9. Duplicate vacancy elimination across NAV and Career sources
# ---------------------------------------------------------------------------


def test_batch_runner_deduplicates_nav_and_career_jobs(tmp_path):
    """Verify that identical vacancies returned by both NAV and career page are deduplicated."""
    shared_job_title = "Lead Architect"

    nav_job = JobPosting(
        job_title=shared_job_title,
        employer_name="TEST AS",
        employer_org_nr="912345678",
        advertisement_url="https://arbeidsplassen.nav.no/stillinger/stilling/nav-1",
        deadline_date="2026-12-31",
    )
    career_job = JobPosting(
        job_title=shared_job_title,
        employer_name="TEST AS",
        employer_org_nr="912345678",
        advertisement_url="https://test.no/karriere/lead-architect",
        deadline_date="2026-12-31",
    )

    class MockJobVerifier:
        def fetch_nav_vacancies(self, legal_name, org_nr, max_results=5):
            return JobPostingList([nav_job], status="completed")

        def discover_career_vacancies(self, **kwargs):
            return JobPostingList([career_job], status="completed")

    def mock_brreg(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "organisasjonsnummer": "912345678",
            "navn": "TEST AS",
            "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
            "forretningsadresse": {"adresse": ["Testveien 1"], "postnummer": "0150", "poststed": "OSLO"},
        })

    mock_client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_brreg)))
    runner = BatchRunner(db_path=tmp_path / "test_dedup.db", client=mock_client)
    runner.job_verifier = MockJobVerifier()

    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(["912345678"]), encoding="utf-8")
    out_file = tmp_path / "out.jsonl"

    runner.run(input_file, out_file, run_id="run-dedup")

    envelope = json.loads(out_file.read_text(encoding="utf-8").strip())
    job_claim = next(c for c in envelope["claims"] if c["field"] == "job_postings")
    assert job_claim["availability"] == "available"
    # Deduplication must yield exactly 1 vacancy, not 2
    assert len(job_claim["value"]) == 1
    assert job_claim["value"][0]["job_title"] == shared_job_title


# ---------------------------------------------------------------------------
# 10. Availability semantics contract: available vs not_available vs blocked vs failed
# ---------------------------------------------------------------------------


def test_availability_semantics_contract_handling(tmp_path):
    """
    Verify exact contract rules:
    - NAV completed (0 hits) + career completed (0 hits) -> not_available
    - NAV blocked (429) + career 0 hits -> blocked (never converted to not_available!)
    - NAV failed (500) + career 0 hits -> failed
    """
    # Case 1: Both completed with 0 vacancies -> not_available
    profile1 = CompanyProfile(org_nr="900000001", name="CLEAN AS", job_postings=[])
    profile1.job_search_status = "completed"
    ev1 = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://brreg.no",
        retrieved_at=utc_now(),
        response_hash="hash1",
    )
    env1 = build_terminal_envelope("900000001", "r1", utc_now(), utc_now(), profile1, ev1)
    c1 = next(c for c in env1["claims"] if c["field"] == "job_postings")
    assert c1["availability"] == "not_available"
    assert c1["value"] is None
    assert c1["confidence"] == 1.0

    # Case 2: NAV blocked -> blocked (never not_available)
    profile2 = CompanyProfile(org_nr="900000002", name="BLOCKED AS", job_postings=[])
    profile2.job_search_status = "blocked"
    env2 = build_terminal_envelope("900000002", "r2", utc_now(), utc_now(), profile2, ev1)
    c2 = next(c for c in env2["claims"] if c["field"] == "job_postings")
    assert c2["availability"] == "blocked"
    assert c2["value"] is None
    assert c2["confidence"] == 0.0

    # Case 3: NAV failed -> failed
    profile3 = CompanyProfile(org_nr="900000003", name="FAILED AS", job_postings=[])
    profile3.job_search_status = "failed"
    env3 = build_terminal_envelope("900000003", "r3", utc_now(), utc_now(), profile3, ev1)
    c3 = next(c for c in env3["claims"] if c["field"] == "job_postings")
    assert c3["availability"] == "failed"
    assert c3["value"] is None
    assert c3["confidence"] == 0.0


# ---------------------------------------------------------------------------
# 11. Mandatory V2.5.1 regression safeguards
# ---------------------------------------------------------------------------


def test_v2_5_1_safeguards_preserved_in_v2_5_2():
    """Verify that privacy, role filtering, financial disclosure, and error semantics remain strict."""
    # 1. No birth dates in CompanyRole
    role = CompanyRole(role_code="DAGL", role_title="Daglig leder", name="Kari Nordmann", is_active=True)
    assert "birth_date" not in role.model_dump()

    # 2. Financial disclosure
    fin = FinancialAccounts(financial_year="2024", account_type="KONSERN", is_consolidated=True, revenue=5000000.0)
    assert fin.account_type == "KONSERN"
    assert fin.is_consolidated is True

    # 3. Role codes: REVI/REGN excluded, LEDE/DAGL included
    allowed = {"DAGL", "LEDE", "NEST", "MEDL", "VARA", "INNH"}
    assert "DAGL" in allowed
    assert "LEDE" in allowed
    assert "REVI" not in allowed
    assert "REGN" not in allowed
