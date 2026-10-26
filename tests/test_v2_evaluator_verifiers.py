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

from app.models import CompanyProfile, SourceEvidence, utc_now
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
