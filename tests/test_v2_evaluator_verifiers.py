"""Automated unit and regression tests for Version 2 evaluator-driven verifications.

Covers all required evaluator scenarios:
- Exact legal entity and organisation number matching on websites.
- Parent group / subsidiary redirection without entity proof (ambiguous).
- Brand vs legal entity distinction.
- Website redirection to unrelated domains.
- Genuine company-owned social profiles vs template / agency / parent handles.
- Genuine dated news articles (RSS & JSON-LD) vs navigation labels and category URLs.
- Rejection of modified dates / feed update times as publication dates.
- Individual job advertisements vs generic careers pages or search queries.
- Employer legal entity matching and parent company vacancy rejection.
- Zero silent drops, closed availability taxonomy, and evidence references.
"""

from datetime import datetime, timezone
import json
import httpx
import pytest

from app.models import (
    CompanyProfile,
    JobPosting,
    NewsArticle,
    SocialProfile,
    SourceEvidence,
    VerifiedWebsite,
    utc_now,
)
from app.services.batch_runner import BatchRunner, build_terminal_envelope
from app.services.job_verifier import JobVerifier
from app.services.news_verifier import NewsVerifier
from app.services.social_verifier import SocialVerifier
from app.services.website_verifier import WebsiteVerifier


# --- A. Legal Company and Website Ownership Tests ---

def test_website_exact_legal_entity_and_org_nr_match():
    """Verify that a candidate site containing the exact 9-digit org nr is verified with high confidence."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head><title>Equinor Official Portal</title></head>
    <body>
        <h1>Equinor ASA</h1>
        <footer>
            <p>Equinor ASA, Forusbeen 50, 4035 Stavanger</p>
            <p>Organisasjonsnummer: 923 609 016 MVA</p>
        </footer>
    </body>
    </html>
    """
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html_content, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = WebsiteVerifier(http_client=client)

    result = verifier.verify_website(
        candidate_url="www.equinor.com",
        org_nr="923609016",
        legal_name="EQUINOR ASA",
    )
    assert result.is_verified is True
    assert result.verification_status == "verified"
    assert result.final_url == "https://www.equinor.com"
    assert "Found organisation number 923609016" in result.evidence_span
    assert result.is_parent_or_group is False


def test_website_parent_company_subsidiary_redirect_ambiguous():
    """
    Verify that when a subsidiary domain redirects to a parent holding group domain
    without containing the target company's organisation number, it is marked as ambiguous
    rather than asserting a false match.
    """
    parent_group_html = """
    <!DOCTYPE html>
    <html>
    <body>
        <h1>Global Parent Energy Group</h1>
        <p>Welcome to our worldwide parent group portal.</p>
        <footer>© Global Parent Holding Ltd. All rights reserved.</footer>
    </body>
    </html>
    """
    def mock_handler(request: httpx.Request) -> httpx.Response:
        # Simulate real HTTP 301 redirect from subsidiary to parent group domain
        if "subsidiaryenergy.no" in str(request.url):
            return httpx.Response(
                301,
                headers={"Location": "https://globalparentenergygroup.com/home"},
                request=request,
            )
        return httpx.Response(
            200,
            text=parent_group_html,
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = WebsiteVerifier(http_client=client)

    result = verifier.verify_website(
        candidate_url="https://subsidiaryenergy.no",
        org_nr="998877665",
        legal_name="SUBSIDIARY ENERGY NORGE AS",
    )
    assert result.is_verified is False
    assert result.verification_status == "ambiguous"
    assert result.is_parent_or_group is True
    assert "Redirected from subsidiaryenergy.no to globalparentenergygroup.com" in result.evidence_span


def test_website_brand_without_legal_entity_evidence():
    """Verify that a site with a brand name but lacking the legal entity name or org nr is marked ambiguous."""
    brand_html = """
    <html><body><h1>Nordic Sparkle Drink</h1><p>Delicious beverages across Scandinavia.</p></body></html>
    """
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=brand_html, request=req)))
    verifier = WebsiteVerifier(http_client=client)

    result = verifier.verify_website(
        candidate_url="https://sparkledrink.no",
        org_nr="912345678",
        legal_name="NORDIC BEVERAGES BOTTLING AS",
    )
    assert result.is_verified is False
    assert result.verification_status == "ambiguous"
    assert "does not present verifiable ownership evidence" in result.evidence_span


def test_website_unreachable_and_timeouts():
    """Verify safe handling of connection drops, HTTP 404, and timeouts."""
    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by host")

    client = httpx.Client(transport=httpx.MockTransport(error_handler))
    verifier = WebsiteVerifier(http_client=client)

    res_err = verifier.verify_website("https://broken-domain.no", "912345678", "TEST AS")
    assert res_err.is_verified is False
    assert res_err.verification_status == "unreachable"

    # Empty candidate URL
    res_empty = verifier.verify_website("", "912345678", "TEST AS")
    assert res_empty.is_verified is False
    assert res_empty.verification_status == "not_available"


# --- B. Social Account Verification Tests ---

def test_social_genuine_company_owned_profile():
    """Verify genuine company LinkedIn and Facebook handles matching legal identity."""
    verifier = SocialVerifier()

    res_li = verifier.verify_social_profile(
        url="https://www.linkedin.com/company/equinor",
        legal_name="EQUINOR ASA",
        org_nr="923609016",
        website_domain="equinor.com",
    )
    assert res_li.is_verified is True
    assert res_li.verification_status == "verified"
    assert res_li.platform == "linkedin"
    assert res_li.handle == "equinor"

    res_fb = verifier.verify_social_profile(
        url="https://facebook.com/EquinorNorge",
        legal_name="EQUINOR ASA",
        org_nr="923609016",
        website_domain="equinor.com",
    )
    assert res_fb.is_verified is True
    assert res_fb.verification_status == "verified"
    assert res_fb.platform == "facebook"


def test_social_reject_template_share_buttons_and_placeholders():
    """Verify that social sharing widgets and theme placeholders are rejected."""
    verifier = SocialVerifier()

    # Sharing buttons
    share_urls = [
        "https://www.facebook.com/sharer/sharer.php?u=https://example.no",
        "https://twitter.com/intent/tweet?text=Hello",
        "https://www.linkedin.com/shareArticle?mini=true&url=https://example.no",
    ]
    for url in share_urls:
        res = verifier.verify_social_profile(url, "TEST AS", "912345678")
        assert res.is_verified is False
        assert res.verification_status == "rejected_template"

    # Placeholder handles
    placeholder_urls = [
        "https://instagram.com/yourcompany",
        "https://facebook.com/companyname",
        "https://twitter.com/username",
    ]
    for url in placeholder_urls:
        res = verifier.verify_social_profile(url, "TEST AS", "912345678")
        assert res.is_verified is False
        assert res.verification_status == "rejected_template"


def test_social_reject_web_development_agency_accounts():
    """Verify rejection of web development / CMS agency accounts linked in website templates."""
    verifier = SocialVerifier()

    agency_urls = [
        "https://facebook.com/wix",
        "https://instagram.com/squarespace",
        "https://linkedin.com/company/maksimer",
    ]
    for url in agency_urls:
        res = verifier.verify_social_profile(url, "BERGEN FISK AS", "912345678", "bergenfisk.no")
        assert res.is_verified is False
        assert res.verification_status == "rejected_agency"


def test_social_misleading_or_unrelated_handle():
    """Verify rejection when handle belongs to an unrelated entity or unconfirmed parent group."""
    verifier = SocialVerifier()

    res = verifier.verify_social_profile(
        url="https://twitter.com/GlobalMegacorpInternational",
        legal_name="LOKAL BUTIKK AS",
        org_nr="912345678",
        website_domain="lokalbutikk.no",
    )
    assert res.is_verified is False
    assert res.verification_status == "rejected_parent"


# --- C. News Article Verification Tests ---

def test_news_genuine_dated_articles_from_rss():
    """Verify extraction of real news articles with individual item pubDate from RSS feed."""
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <title>Equinor News</title>
            <link>https://www.equinor.com</link>
            <lastBuildDate>Mon, 26 Oct 2026 09:00:00 GMT</lastBuildDate>
            <item>
                <title>Equinor awards major offshore wind contract</title>
                <link>https://www.equinor.com/news/20261020-offshore-wind</link>
                <pubDate>Tue, 20 Oct 2026 08:30:00 GMT</pubDate>
            </item>
            <item>
                <title>Third quarter 2026 financial results published</title>
                <link>https://www.equinor.com/news/20261025-q3-results</link>
                <pubDate>2026-10-25</pubDate>
            </item>
        </channel>
    </rss>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_rss(rss_xml, "equinor.com")

    assert len(articles) == 2
    assert articles[0].title == "Equinor awards major offshore wind contract"
    assert articles[0].publication_date == "2026-10-20"
    assert articles[0].article_url == "https://www.equinor.com/news/20261020-offshore-wind"
    assert articles[1].publication_date == "2026-10-25"


def test_news_reject_navigation_labels_and_category_pages():
    """Verify that generic navigation labels (e.g. 'See more news', 'Archive') and category URLs are rejected."""
    rss_with_junk = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <item>
                <title>See all news</title>
                <link>https://example.no/news/</link>
                <pubDate>2026-10-20</pubDate>
            </item>
            <item>
                <title>Les mer</title>
                <link>https://example.no/archive</link>
                <pubDate>2026-10-20</pubDate>
            </item>
            <item>
                <title>New energy storage technology deployed in Rogaland</title>
                <link>https://example.no/news/new-energy-storage-deployed</link>
                <pubDate>2026-10-22</pubDate>
            </item>
        </channel>
    </rss>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_rss(rss_with_junk, "example.no")

    assert len(articles) == 1
    assert articles[0].title == "New energy storage technology deployed in Rogaland"


def test_news_reject_undated_and_modified_date():
    """Verify rule: Reject undated items and do not accept dateModified as publication date."""
    html_json_ld = """
    <html>
    <head>
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Historic milestone reached for renewable project",
        "url": "https://example.no/news/historic-milestone",
        "dateModified": "2026-10-26T10:00:00Z"
    }
    </script>
    </head>
    <body></body>
    </html>
    """
    verifier = NewsVerifier()
    # No datePublished provided (only dateModified) -> must be rejected
    articles = verifier.extract_from_html(html_json_ld, "https://example.no", "example.no")
    assert len(articles) == 0


def test_news_deduplication():
    """Verify that duplicate or syndicated articles with the same title are not counted as separate events."""
    rss_with_dups = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <item>
                <title>Quarterly Results Presentation 2026</title>
                <link>https://example.no/news/q3-1</link>
                <pubDate>2026-10-20</pubDate>
            </item>
            <item>
                <title>Quarterly Results Presentation 2026!</title>
                <link>https://example.no/news/q3-2</link>
                <pubDate>2026-10-20</pubDate>
            </item>
        </channel>
    </rss>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_rss(rss_with_dups, "example.no")
    assert len(articles) == 1


# --- D. Individual Job Advertisement Verification Tests ---

def test_job_individual_ad_for_target_company():
    """Verify extraction of official active individual vacancy with target employer match."""
    mock_payload = {
        "took": 15,
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_source": {
                        "uuid": "abc-123-uuid",
                        "title": "Senior Reservoir Engineer",
                        "status": "ACTIVE",
                        "businessName": "Equinor ASA",
                        "employer": {"name": "EQUINOR ASA AVD STAVANGER"},
                        "published": "2026-10-15T08:00:00Z",
                        "expires": "2026-11-15T23:59:59Z",
                        "locationList": [{"city": "STAVANGER", "municipal": "STAVANGER"}],
                        "properties": {"applicationdue": "15.11.2026"},
                    }
                }
            ],
        },
    }
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_payload)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    postings = verifier.fetch_nav_vacancies("EQUINOR ASA", "923609016")
    assert len(postings) == 1
    job = postings[0]
    assert job.job_title == "Senior Reservoir Engineer"
    assert job.employer_name == "EQUINOR ASA AVD STAVANGER"
    assert job.advertisement_url == "https://arbeidsplassen.nav.no/stillinger/stilling/abc-123-uuid"
    assert job.location == "STAVANGER"
    assert job.publication_date == "2026-10-15"
    assert job.deadline_date == "15.11.2026"
    assert job.is_active is True


def test_job_reject_generic_careers_landing_pages():
    """Verify rejection of generic career landing pages and search queries as job vacancies."""
    assert JobVerifier.is_generic_careers_url("https://example.no/karriere") is True
    assert JobVerifier.is_generic_careers_url("https://example.no/careers/") is True
    assert JobVerifier.is_generic_careers_url("https://example.no/stillinger") is True
    assert JobVerifier.is_generic_careers_url("https://arbeidsplassen.nav.no/stillinger?q=developer") is True
    # Individual posting should NOT be rejected as generic
    assert JobVerifier.is_generic_careers_url("https://arbeidsplassen.nav.no/stillinger/stilling/abc-123") is False


def test_job_reject_parent_or_unrelated_employer_vacancy():
    """Verify that vacancies belonging to a different entity or staffing agency are not misattributed."""
    mock_payload = {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_source": {
                        "uuid": "xyz-789",
                        "title": "Warehouse Worker",
                        "status": "ACTIVE",
                        "businessName": "Adecco Staffing Solutions",
                        "employer": {"name": "Adecco AS"},
                        "published": "2026-10-10",
                    }
                }
            ],
        }
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=mock_payload)))
    verifier = JobVerifier(http_client=client)

    postings = verifier.fetch_nav_vacancies("BERGEN LOGISTIKK AS", "987654321")
    assert len(postings) == 0  # Adecco vacancy rejected for Bergen Logistikk AS!


def test_job_expired_vacancy_ignored():
    """Verify that expired job postings are excluded from active vacancies."""
    mock_payload = {
        "hits": {
            "total": {"value": 1},
            "hits": [
                {
                    "_source": {
                        "uuid": "expired-uuid-1",
                        "title": "Summer Intern 2024",
                        "status": "ACTIVE",
                        "businessName": "Equinor ASA",
                        "employer": {"name": "EQUINOR ASA"},
                        "expires": "2024-05-01T00:00:00Z",  # In the past
                    }
                }
            ],
        }
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=mock_payload)))
    verifier = JobVerifier(http_client=client)

    postings = verifier.fetch_nav_vacancies("EQUINOR ASA", "923609016")
    assert len(postings) == 0


# --- E. Terminal Envelope V2 Integration Tests ---

def test_envelope_v2_claims_and_evidence_integration(tmp_path):
    """
    Verify complete integration in build_terminal_envelope:
    - 20 base registry claims preserved.
    - 4 Version 2 extended claims added with verified evidence sources.
    - Proper source_class (official_registry, company_owned, public_employment_service).
    - Closed availability states.
    """
    from app.models import JobPosting, NewsArticle, SocialProfile, VerifiedWebsite

    profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        org_form="ASA",
        org_form_description="Allment aksjeselskap",
        registration_date="1997-09-08",
        municipality="STAVANGER",
        employees=21000,
        official_website="www.equinor.com",
        verified_website=VerifiedWebsite(
            candidate_url="www.equinor.com",
            final_url="https://www.equinor.com",
            is_verified=True,
            verification_status="verified",
            evidence_span="Found organisation number 923609016 on page footer.",
        ),
        social_profiles=[
            SocialProfile(
                platform="linkedin",
                profile_url="https://linkedin.com/company/equinor",
                handle="equinor",
                is_verified=True,
                verification_status="verified",
            )
        ],
        news_articles=[
            NewsArticle(
                title="Equinor reports third quarter results",
                article_url="https://www.equinor.com/news/q3-results",
                source="equinor.com",
                publication_date="2026-10-25",
            )
        ],
        job_postings=[
            JobPosting(
                job_title="Geoscientist",
                employer_name="EQUINOR ASA",
                advertisement_url="https://arbeidsplassen.nav.no/stillinger/stilling/geo-123",
                location="STAVANGER",
                deadline_date="2026-11-20",
                is_active=True,
            )
        ],
    )

    evidence = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        response_hash="hash123",
    )

    envelope = build_terminal_envelope(
        org_nr="923609016",
        run_id="v2-test-run",
        started_at=utc_now(),
        completed_at=utc_now(),
        profile=profile,
        evidence=evidence,
    )

    claims = {c["field"]: c for c in envelope["claims"]}
    ev_by_id = {e["id"]: e for e in envelope["evidence"]}

    # Base registry claims present
    assert "legal_name" in claims
    assert claims["legal_name"]["value"] == "EQUINOR ASA"
    assert "employees" in claims
    assert claims["employees"]["value"] == 21000

    # V2 Extended Claims present
    assert "verified_website" in claims
    assert claims["verified_website"]["value"] == "https://www.equinor.com"
    assert claims["verified_website"]["availability"] == "available"
    assert claims["verified_website"]["confidence"] == 1.0
    ev_vw_id = claims["verified_website"]["evidence_ids"][0]
    assert ev_by_id[ev_vw_id]["source_class"] == "company_owned"

    assert "social_profiles" in claims
    assert claims["social_profiles"]["value"] == ["https://linkedin.com/company/equinor"]
    assert claims["social_profiles"]["availability"] == "available"

    assert "news_articles" in claims
    assert len(claims["news_articles"]["value"]) == 1
    assert claims["news_articles"]["value"][0]["title"] == "Equinor reports third quarter results"
    assert claims["news_articles"]["value"][0]["publication_date"] == "2026-10-25"

    assert "job_postings" in claims
    assert len(claims["job_postings"]["value"]) == 1
    assert claims["job_postings"]["value"][0]["job_title"] == "Geoscientist"
    assert claims["job_postings"]["availability"] == "available"
    ev_job_id = claims["job_postings"]["evidence_ids"][0]
    assert ev_by_id[ev_job_id]["source_class"] == "public_employment_service"


def test_envelope_v2_zero_jobs_and_empty_assets_not_available():
    """Verify that a checked source with 0 jobs emits not_available (not available)."""
    profile = CompanyProfile(
        org_nr="923609016",
        name="EQUINOR ASA",
        official_website="https://www.equinor.com",
        verified_website=VerifiedWebsite(
            candidate_url="https://www.equinor.com",
            final_url="https://www.equinor.com",
            is_verified=True,
            verification_status="verified",
        ),
        social_profiles=[],
        news_articles=[],
        job_postings=[],  # 0 jobs from NAV
    )

    evidence = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        response_hash="hash123",
    )

    envelope = build_terminal_envelope(
        org_nr="923609016",
        run_id="v2-test-run",
        started_at=utc_now(),
        completed_at=utc_now(),
        profile=profile,
        evidence=evidence,
    )

    claims = {c["field"]: c for c in envelope["claims"]}
    ev_by_id = {e["id"]: e for e in envelope["evidence"]}

    # Checked job postings with 0 jobs must be 'not_available', backed by Arbeidsplassen evidence
    assert "job_postings" in claims
    assert claims["job_postings"]["availability"] == "not_available"
    assert claims["job_postings"]["value"] is None
    assert len(claims["job_postings"]["evidence_ids"]) == 1
    ev_id = claims["job_postings"]["evidence_ids"][0]
    assert "0 active vacancies found" in ev_by_id[ev_id]["claim_span"]

    # Empty social profiles and news must be 'not_available'
    assert "social_profiles" in claims
    assert claims["social_profiles"]["availability"] == "not_available"
    assert claims["social_profiles"]["value"] is None

    assert "news_articles" in claims
    assert claims["news_articles"]["availability"] == "not_available"
    assert claims["news_articles"]["value"] is None


def test_batch_runner_wires_social_and_news_from_verified_website(tmp_path):
    """Test that batch_runner automatically extracts verified social links and news from verified homepage."""
    from app.services.batch_runner import BatchRunner
    from app.services.database import DatabaseService

    html_content = """
    <html>
      <head>
        <title>EQUINOR ASA</title>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "Equinor expands offshore wind capacity",
          "url": "https://www.equinor.com/news/offshore-wind",
          "datePublished": "2026-10-20T08:00:00Z"
        }
        </script>
      </head>
      <body>
        <p>Org.nr: 923 609 016</p>
        <footer>
          <a href="https://linkedin.com/company/equinor">LinkedIn</a>
          <a href="https://facebook.com/sharer.php?u=foo">Share</a>
        </footer>
      </body>
    </html>
    """

    class MockBrregClient:
        def fetch_company(self, org_nr):
            prof = CompanyProfile(
                org_nr=org_nr,
                name="EQUINOR ASA",
                official_website="https://www.equinor.com",
            )
            ev = SourceEvidence(
                source_name="Enhetsregisteret",
                source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_nr}",
                response_hash="abc",
            )
            return prof, ev

    class MockWebsiteVerifier:
        def extract_domain(self, url):
            return "equinor.com"

        def verify_website(self, candidate_url, org_nr, legal_name, *args, **kwargs):
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=candidate_url,
                is_verified=True,
                verification_status="verified",
                html_content=html_content,
            )

    class MockJobVerifier:
        def fetch_nav_vacancies(self, legal_name, org_nr):
            return []

    db = DatabaseService(tmp_path / "test.db")
    runner = BatchRunner(db=db, client=MockBrregClient())
    runner.website_verifier = MockWebsiteVerifier()
    runner.job_verifier = MockJobVerifier()

    envelope = runner.process_company("923609016", "test-run")

    claims = {c["field"]: c for c in envelope["claims"]}

    # Verified website
    assert claims["verified_website"]["availability"] == "available"
    assert claims["verified_website"]["value"] == "https://www.equinor.com"

    # Social profile extracted & verified (share link filtered out)
    assert claims["social_profiles"]["availability"] == "available"
    assert claims["social_profiles"]["value"] == ["https://linkedin.com/company/equinor"]

    # News article extracted & verified
    assert claims["news_articles"]["availability"] == "available"
    assert len(claims["news_articles"]["value"]) == 1
    assert claims["news_articles"]["value"][0]["title"] == "Equinor expands offshore wind capacity"
    assert claims["news_articles"]["value"][0]["publication_date"] == "2026-10-20"

    # 0 jobs -> not_available
    assert claims["job_postings"]["availability"] == "not_available"
    assert claims["job_postings"]["value"] is None


def test_job_verifier_strips_legal_suffix_and_discovers_vacancies():
    """Verify that JobVerifier strips corporate form suffixes and verifies employer matches."""
    # Employer match verification tests
    assert JobVerifier.verify_employer_match("Telenor Norge AS", "TELENOR ASA") is True
    assert JobVerifier.verify_employer_match("Telenor Group", "TELENOR ASA") is True
    assert JobVerifier.verify_employer_match("Veidekke Entreprenør AS", "VEIDEKKE ASA") is True
    assert JobVerifier.verify_employer_match("Veidekke Bygg", "VEIDEKKE ASA") is True

    # Rejection of unrelated organizations with shared brand or religious terms
    assert JobVerifier.verify_employer_match("Telenor Arena Idrettslag", "TELENOR ASA") is False
    assert JobVerifier.verify_employer_match("Bamble Menighet", "TELENOR ASA") is False
    assert JobVerifier.verify_employer_match("Equinor Pensjonistforening", "EQUINOR ASA") is False

    # Mock NAV search endpoint
    recorded_urls = []

    class MockNavClient:
        def get(self, url, headers=None):
            recorded_urls.append(url)
            # Return valid vacancy response
            payload = {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "uuid": "job-abc-123",
                                "title": "Senior Network Engineer",
                                "status": "ACTIVE",
                                "businessName": "Telenor Norge AS",
                                "employer": {"name": "Telenor Norge AS"},
                                "expires": "2026-12-31T23:59:59",
                                "published": "2026-10-15T08:00:00",
                                "locationList": [{"municipal": "OSLO"}],
                            }
                        }
                    ]
                }
            }
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    verifier = JobVerifier(http_client=MockNavClient())
    postings = verifier.fetch_nav_vacancies("TELENOR ASA", "976632063")

    # Confirms query used clean base name 'TELENOR' without 'ASA'
    assert any("q=TELENOR" in u and "ASA" not in u for u in recorded_urls)
    assert len(postings) == 1
    assert postings[0].job_title == "Senior Network Engineer"
    assert postings[0].employer_name == "Telenor Norge AS"
    assert postings[0].advertisement_url == "https://arbeidsplassen.nav.no/stillinger/stilling/job-abc-123"


def test_news_homepage_false_positive_rejected():
    """Verify that root landing pages with article metadata are rejected as news articles."""
    # URL level checks
    assert NewsVerifier.is_homepage_url("https://www.yara.com/") is True
    assert NewsVerifier.is_homepage_url("https://www.yara.com") is True
    assert NewsVerifier.is_homepage_url("https://www.yara.com/en") is True
    assert NewsVerifier.is_homepage_url("https://www.yara.com/en/") is True
    assert NewsVerifier.is_homepage_url("https://www.yara.com/no/forside") is True
    assert NewsVerifier.is_homepage_url("https://www.yara.com/index.html") is True

    # Real article paths should NOT be flagged as homepages
    assert NewsVerifier.is_homepage_url("https://www.yara.com/news/q3-results") is False
    assert NewsVerifier.is_homepage_url("https://www.yara.com/press-releases/2026/fertilizer") is False

    # HTML extraction rejection test: root page claiming to be an article
    homepage_html = """<!DOCTYPE html>
    <html>
    <head>
        <title>Yara International - Knowledge Grows</title>
        <meta property="og:type" content="article" />
        <meta property="og:url" content="https://www.yara.com/" />
        <meta property="og:title" content="Knowledge Grows - Yara International" />
        <meta property="article:published_time" content="2022-07-01T12:00:00Z" />
    </head>
    <body>Welcome to Yara</body>
    </html>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_html(homepage_html, "https://www.yara.com/")
    # Root homepage should be rejected, resulting in 0 articles
    assert len(articles) == 0


def test_news_section_link_discovery():
    """Verify that first-hop internal news links are discovered from homepage HTML."""
    html = """<!DOCTYPE html>
    <html>
    <body>
        <nav>
            <a href="/om-oss">Om oss</a>
            <a href="/nyheter">Aktuelt og Nyheter</a>
            <a href="/karriere">Karriere</a>
            <a href="https://external-news.com/news">External</a>
        </nav>
    </body>
    </html>
    """
    section_url = NewsVerifier.find_news_section_url(html, "https://example.com")
    assert section_url == "https://example.com/nyheter"

    # Reject when no news section exists
    no_news_html = "<html><body><a href='/about'>About</a></body></html>"
    assert NewsVerifier.find_news_section_url(no_news_html, "https://example.com") is None


def test_website_candidate_without_org_nr_or_legal_name_marked_ambiguous():
    """Verify that candidate websites without exact org nr or legal name are safely marked ambiguous."""
    html = """<!DOCTYPE html>
    <html>
    <head><title>Mowi - Leading the Blue Revolution</title></head>
    <body>
        <h1>Welcome to Mowi Global</h1>
        <p>Sustainable aquaculture and salmon farming.</p>
    </body>
    </html>
    """
    class MockClient:
        def get(self, url, headers=None, follow_redirects=True):
            return httpx.Response(200, text=html, request=httpx.Request("GET", url))

    verifier = WebsiteVerifier(http_client=MockClient())

    # Candidate website without exact legal entity or org number proof is safely marked ambiguous
    vw = verifier.verify_website(
        "https://www.mowi.com",
        org_nr="922401667",
        legal_name="MOWI ASA",
    )
    assert vw.is_verified is False
    assert vw.verification_status == "ambiguous"


def test_website_discovery_for_missing_registry_urls():
    """Verify candidate domain discovery for companies missing registry websites."""
    aker_html = """<!DOCTYPE html>
    <html>
    <head><title>Aker ASA - Industrial investment company</title></head>
    <body>
        <footer>Aker ASA | Org.nr: 886 581 432 | Oksenøyveien 10, 1366 Lysaker</footer>
    </body>
    </html>
    """

    class MockClient:
        def get(self, url, headers=None, follow_redirects=True):
            if "akerasa.com" in url or "aker.no" in url:
                return httpx.Response(200, text=aker_html, request=httpx.Request("GET", url))
            return httpx.Response(404, request=httpx.Request("GET", url))

    verifier = WebsiteVerifier(http_client=MockClient())

    # Discovery should probe candidate domains and find akerasa.com with verified org nr
    vw = verifier.discover_candidate_domain(
        legal_name="AKER ASA",
        org_nr="886581432",
    )
    assert vw is not None
    assert vw.is_verified is True
    assert "886581432" in vw.evidence_span or "886 581 432" in vw.evidence_span


def test_job_employer_mismatch_rejection_voith_hydro():
    """Verify that a distinct company with partial token overlap (Voith Hydro) is rejected for Norsk Hydro."""
    assert JobVerifier.verify_employer_match("VOITH HYDRO AS AVD FREDRIKSTAD", "NORSK HYDRO ASA") is False
    assert JobVerifier.verify_employer_match("NORSK HYDRO ENERGI AS", "NORSK HYDRO ASA") is True
    assert JobVerifier.verify_employer_match("HYDRO REIN AS", "NORSK HYDRO ASA") is True
    assert JobVerifier.verify_employer_match("SIEMENS ENERGY AS", "EQUINOR ASA") is False


def test_job_rate_limited_blocked_state():
    """Verify that HTTP 429 returns blocked status and builds a blocked availability claim."""
    class MockRateLimitedClient:
        def get(self, url, headers=None):
            return httpx.Response(429, request=httpx.Request("GET", url))

    verifier = JobVerifier(http_client=MockRateLimitedClient())
    postings = verifier.fetch_nav_vacancies("TELENOR ASA", "982463718")
    assert postings.status == "blocked"
    assert len(postings) == 0

    profile = CompanyProfile(
        org_nr="982463718",
        name="TELENOR ASA",
        job_postings=postings,
        job_search_status="blocked",
    )
    evidence = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/982463718",
        response_hash="hash123",
    )
    envelope = build_terminal_envelope(
        org_nr="982463718",
        run_id="test-blocked",
        started_at=utc_now(),
        completed_at=utc_now(),
        profile=profile,
        evidence=evidence,
    )
    claims = {c["field"]: c for c in envelope["claims"]}
    assert claims["job_postings"]["availability"] == "blocked"
    assert claims["job_postings"]["value"] is None
    assert claims["job_postings"]["confidence"] == 0.0


def test_news_rss_feed_link_extraction():
    """Verify that corporate RSS feed links are discovered from HTML header."""
    html = """<!DOCTYPE html>
    <html>
    <head>
        <link rel="alternate" type="application/rss+xml" title="Bouvet deler" href="https://www.bouvet.no/bouvet-deler/rss" />
        <link rel="alternate" type="application/rss+xml" title="Comments" href="https://www.bouvet.no/comments/feed" />
        <link rel="alternate" type="application/rss+xml" title="External" href="https://othercompany.com/feed" />
    </head>
    <body>Welcome</body>
    </html>
    """
    feeds = NewsVerifier.find_rss_feed_urls(html, "https://www.bouvet.no/")
    assert feeds == ["https://www.bouvet.no/bouvet-deler/rss"]


def test_news_semantic_time_tag_extraction():
    """Verify that semantic HTML article cards with <time datetime="..."> are extracted."""
    html = """<!DOCTYPE html>
    <html>
    <body>
        <article class="news-item">
            <h2><a href="/nyheter/stromnettet-oppgraderes">Statnett oppgraderer kraftnettet i Innlandet</a></h2>
            <time datetime="2026-10-26 12:01:27Z">26. oktober 2026</time>
        </article>
    </body>
    </html>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_html(html, "https://www.statnett.no/nyheter")
    assert len(articles) == 1
    assert articles[0].title == "Statnett oppgraderer kraftnettet i Innlandet"
    assert articles[0].publication_date == "2026-10-26"
    assert articles[0].article_url == "https://www.statnett.no/nyheter/stromnettet-oppgraderes"


def test_news_not_applicable_when_no_website():
    """Verify that companies without a verified website report news as not_applicable."""
    profile = CompanyProfile(
        org_nr="988775460",
        name="RØDSKIFERVEIEN INVEST AS",
        verified_website=None,
        news_search_status="not_applicable",
    )
    evidence = SourceEvidence(
        source_name="Enhetsregisteret",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/988775460",
        response_hash="hash123",
    )
    envelope = build_terminal_envelope(
        org_nr="988775460",
        run_id="test-na",
        started_at=utc_now(),
        completed_at=utc_now(),
        profile=profile,
        evidence=evidence,
    )
    claims = {c["field"]: c for c in envelope["claims"]}
    assert claims["news_articles"]["availability"] == "not_applicable"
    assert claims["news_articles"]["value"] is None


def test_news_reject_store_locator_and_opening_hours():
    """Verify that operational store-locator pages (e.g. /finn-butikk) and generic titles are rejected."""
    html = """<!DOCTYPE html>
    <html>
    <body>
        <article class="store-item">
            <h2><a href="/finn-butikk">Alle butikker og åpningstider</a></h2>
            <time datetime="2026-11-04">4. november 2026</time>
        </article>
    </body>
    </html>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_html(html, "https://www.vinmonopolet.no")
    assert len(articles) == 0


def test_news_rss_feed_html_entity_unescaping():
    """Verify that HTML entities in news titles are properly unescaped."""
    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
        <channel>
            <title>Statnett Pressemeldinger</title>
            <link>https://www.statnett.no</link>
            <item>
                <title>Statnett planlegger en ny str&#xF8;mforbindelse p&#xE5; Hadeland</title>
                <link>https://www.statnett.no/nyheter/stromforbindelse</link>
                <pubDate>Mon, 26 Oct 2026 12:00:00 GMT</pubDate>
            </item>
        </channel>
    </rss>
    """
    verifier = NewsVerifier()
    articles = verifier.extract_from_rss(rss, "statnett.no")
    assert len(articles) == 1
    assert articles[0].title == "Statnett planlegger en ny strømforbindelse på Hadeland"
    assert articles[0].publication_date == "2026-10-26"


# --- G. Version 2.3 Improvements: Cache, Sub-page Verification, & News Archive Tests ---

def test_job_search_cache_hit_and_miss(tmp_path):
    """Verify that NAV job search results are persisted in SQLite and served on subsequent lookups."""
    from app.services.database import DatabaseService

    db = DatabaseService(tmp_path / "cache_test.db")
    
    mock_payload = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "uuid": "test-uuid-123",
                        "title": "Senior Cloud Engineer",
                        "status": "ACTIVE",
                        "businessName": "Telenor ASA",
                        "employer": {"name": "Telenor ASA"},
                        "locationList": [{"city": "Fornebu", "municipal": "Bærum"}],
                        "published": "2026-10-20T10:00:00Z",
                        "expires": "2026-11-20T10:00:00Z",
                    }
                }
            ]
        }
    }

    call_count = 0
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=mock_payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client, db=db, cache_ttl_seconds=3600)

    # First call: cache miss, triggers HTTP call
    res1 = verifier.fetch_nav_vacancies(legal_name="TELENOR ASA", org_nr="976967631")
    assert res1.status == "completed"
    assert len(res1) == 1
    assert res1[0].job_title == "Senior Cloud Engineer"
    assert call_count == 1

    # Second call: cache hit, served directly from SQLite without network call
    res2 = verifier.fetch_nav_vacancies(legal_name="TELENOR ASA", org_nr="976967631")
    assert res2.status == "completed"
    assert len(res2) == 1
    assert res2[0].job_title == "Senior Cloud Engineer"
    assert call_count == 1  # No additional network call!


def test_job_search_cache_expiry(tmp_path):
    """Verify that expired cache entries are discarded and re-fetched."""
    from app.services.database import DatabaseService

    db = DatabaseService(tmp_path / "cache_exp_test.db")

    # Insert an expired entry directly into SQLite
    db.set_job_search_cache(
        cache_key="976967631",
        org_nr="976967631",
        legal_name="TELENOR ASA",
        search_status="completed",
        vacancies=[{"job_title": "Old Job", "employer_name": "Telenor ASA", "advertisement_url": "https://ad.no"}],
        retrieved_at="2026-10-01T00:00:00Z",
        expires_at="2026-10-02T00:00:00Z",  # In the past
    )

    call_count = 0
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"hits": {"hits": []}}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client, db=db, cache_ttl_seconds=3600)

    # Should detect expired entry, return None from cache, and execute network query (for both terms when 0 hits)
    res = verifier.fetch_nav_vacancies(legal_name="TELENOR ASA", org_nr="976967631")
    assert res.status == "completed"
    assert len(res) == 0
    assert call_count >= 1


def test_job_search_never_caches_blocked_or_failed(tmp_path):
    """Verify that HTTP 429 blocked or network error states are NEVER saved into the cache as successful."""
    from app.services.database import DatabaseService

    db = DatabaseService(tmp_path / "cache_blocked_test.db")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client, db=db, cache_ttl_seconds=3600)

    res = verifier.fetch_nav_vacancies(legal_name="EQUINOR ASA", org_nr="923609016")
    assert res.status == "blocked"

    # Verify database has NO cached entry for this org_nr
    cached = db.get_job_search_cache("923609016")
    assert cached is None


def test_job_search_cache_corruption_resilience(tmp_path):
    """Verify that corrupt JSON in SQLite cache does not crash job verification."""
    from app.services.database import DatabaseService

    db = DatabaseService(tmp_path / "cache_corrupt_test.db")
    conn = db.get_connection()
    with conn:
        conn.cursor().execute(
            """
            INSERT INTO job_search_cache (
                cache_key, org_nr, legal_name, search_status, vacancies_json,
                retrieved_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("923609016", "923609016", "EQUINOR ASA", "completed", "NOT_JSON{{{", "2026-10-20T00:00:00Z", "2099-01-01T00:00:00Z"),
        )
    conn.close()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": []}}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client, db=db)

    # Should not raise exception
    res = verifier.fetch_nav_vacancies(legal_name="EQUINOR ASA", org_nr="923609016")
    assert res.status == "completed"


def test_job_rate_limit_retry_after_handling():
    """Verify that Retry-After header is parsed and bounded when rate limited."""
    attempts = 0
    hit_payload = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "uuid": "bouvet-1",
                        "title": "Konsulent",
                        "status": "ACTIVE",
                        "employer": {"name": "Bouvet ASA"},
                        "locationList": [{"city": "Oslo"}],
                    }
                }
            ]
        }
    }
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, request=request)
        return httpx.Response(200, json=hit_payload, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = JobVerifier(http_client=client)

    res = verifier.fetch_nav_vacancies(legal_name="BOUVET ASA", org_nr="982363062")
    assert attempts == 2
    assert res.status == "completed"
    assert len(res) == 1
    assert res[0].job_title == "Konsulent"


def test_website_discovery_subpage_verification_veidekke_storebrand():
    """
    Verify that when candidate domain homepage omits legal suffix 'ASA' (ambiguous),
    deeper inspection of /kontakt or /investor sub-pages recovers exact legal name or org nr.
    """
    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == "https://www.veidekke.no" or url_str == "https://www.veidekke.no/":
            # Homepage has brand "Veidekke" but lacks "Veidekke ASA" or org nr
            return httpx.Response(200, text="<html><body><h1>Veidekke</h1><p>Bygger fremtiden</p></body></html>", request=request)
        elif "/kontakt" in url_str:
            # /kontakt has exact full legal name and org number
            return httpx.Response(200, text="<html><body><h1>Kontakt oss</h1><p>Veidekke ASA</p><p>Org.nr. 921 785 410</p></body></html>", request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    verifier = WebsiteVerifier(http_client=client)

    vw = verifier.discover_candidate_domain(legal_name="VEIDEKKE ASA", org_nr="921785410")
    assert vw is not None
    assert vw.is_verified is True
    assert vw.verification_status == "verified"
    assert "921785410" in vw.evidence_span or "VEIDEKKE ASA" in vw.evidence_span


def test_news_section_expanded_paths_and_subpage_rss():
    """Verify that find_news_section_url recognizes Norwegian corporate press paths and rejects store locators."""
    verifier = NewsVerifier()
    
    html = """
    <html>
    <body>
        <a href="/media/pressemeldinger/">Pressemeldinger</a>
        <a href="/finn-butikk">Finn butikk</a>
        <a href="/karriere">Karriere</a>
    </body>
    </html>
    """
    found_url = verifier.find_news_section_url(html, "https://www.orkla.no")
    assert found_url == "https://www.orkla.no/media/pressemeldinger/"

    # Store locator and opening hours are rejected
    store_locator_html = '<a href="/finn-butikk/oslo">Finn butikk</a>'
    assert verifier.find_news_section_url(store_locator_html, "https://www.vinmonopolet.no") is None





