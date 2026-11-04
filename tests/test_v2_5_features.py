"""Unit tests for Signalpost Agent V2.5 enhancements:
- Phase 1: Official Company Management Data (Brønnøysund Roller API)
- Phase 2: Corporate News OpenGraph Recovery (attribute order & metadata)
- Phase 3: Career-Page Schema.org JobPosting Fallback
- Phase 4: Financial Accounts and Results (Regnskapsregisteret API)
- Phase 5: Synthesis & Evaluation Contract Compliance
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import httpx
import pytest

from app.models import CompanyProfile, CompanyRole, FinancialAccounts, SourceEvidence, utc_now
from app.services.batch_runner import BatchRunner, build_terminal_envelope
from app.services.brreg_client import BrregClient
from app.services.job_verifier import JobVerifier
from app.services.news_verifier import NewsVerifier


FIXTURES_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Phase 1: Brønnøysund Roller API Tests
# ---------------------------------------------------------------------------


def test_fetch_roles_equinor_fixture():
    """Verify live fixture extraction for Equinor ASA (org nr 923609016)."""
    fixture_path = FIXTURES_DIR / "fixtures_roles_923609016.json"
    raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "923609016/roller" in str(request.url):
            return httpx.Response(200, json=raw_data)
        return httpx.Response(404)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    roles, evidence, status = client.fetch_roles("923609016")

    assert status == "completed"
    assert len(roles) >= 2
    assert evidence is not None
    assert evidence.source_name == "Brønnøysund Register Centre (Roller)"
    assert "923609016/roller" in evidence.source_url

    # Daglig leder (CEO): Anders Opedal
    dagl = next((r for r in roles if r.role_code == "DAGL"), None)
    assert dagl is not None
    assert dagl.name == "Anders Opedal"
    assert dagl.role_title == "Daglig leder"
    assert dagl.is_active is True

    # Styreleder (Chair): Jarle Kjell Roth
    chair = next((r for r in roles if r.role_code == "LEDE"), None)
    assert chair is not None
    assert chair.name == "Jarle Kjell Roth"
    assert chair.role_title == "Styrets leder"


def test_fetch_roles_dnb_fixture():
    """Verify live fixture extraction for DNB Bank ASA (org nr 984851006)."""
    fixture_path = FIXTURES_DIR / "fixtures_roles_984851006.json"
    raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "984851006/roller" in str(request.url):
            return httpx.Response(200, json=raw_data)
        return httpx.Response(404)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    roles, evidence, status = client.fetch_roles("984851006")

    assert status == "completed"
    # CEO: Kjerstin Braathen (full legal name in registry: Kjerstin Elisabeth Rasmussen Braathen)
    dagl = next((r for r in roles if r.role_code == "DAGL"), None)
    assert dagl is not None
    assert dagl.name == "Kjerstin Elisabeth Rasmussen Braathen"

    # Chair: Eimund Nygaard
    chair = next((r for r in roles if r.role_code == "LEDE"), None)
    assert chair is not None
    assert chair.name == "Eimund Nygaard"


def test_fetch_roles_bouvet_fixture():
    """Verify live fixture extraction for Bouvet ASA (org nr 974442167)."""
    fixture_path = FIXTURES_DIR / "fixtures_roles_974442167.json"
    raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "974442167/roller" in str(request.url):
            return httpx.Response(200, json=raw_data)
        return httpx.Response(404)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    roles, evidence, status = client.fetch_roles("974442167")

    assert status == "completed"
    # CEO: Per Gunnar Tronsli
    dagl = next((r for r in roles if r.role_code == "DAGL"), None)
    assert dagl is not None
    assert dagl.name == "Per Gunnar Tronsli"

    # Chair: Pål Egil Rønn
    chair = next((r for r in roles if r.role_code == "LEDE"), None)
    assert chair is not None
    assert chair.name == "Pål Egil Rønn"


def test_fetch_roles_filters_deregistered_and_deceased():
    """Ensure deregistered roles and deceased persons are never accepted."""
    synthetic_data = {
        "_links": {
            "enhet": {"href": "https://data.brreg.no/enhetsregisteret/api/enheter/111222333"}
        },
        "rollegrupper": [
            {
                "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {
                            "navn": {"fornavn": "Aktiv", "etternavn": "Leder"},
                            "erDoed": False,
                        },
                        "avregistrert": False,
                    },
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Tidligere leder"},
                        "person": {
                            "navn": {"fornavn": "Gammel", "etternavn": "Leder"},
                            "erDoed": False,
                        },
                        "avregistrert": True,  # Deregistered -> must be skipped
                    },
                    {
                        "type": {"kode": "LEDE", "beskrivelse": "Styrets leder"},
                        "person": {
                            "navn": {"fornavn": "Avdød", "etternavn": "Person"},
                            "erDoed": True,  # Deceased -> must be skipped
                        },
                        "avregistrert": False,
                    },
                ],
            }
        ],
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=synthetic_data)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    roles, evidence, status = client.fetch_roles("111222333")

    assert status == "completed"
    assert len(roles) == 1
    assert roles[0].name == "Aktiv Leder"


def test_fetch_roles_error_handling():
    """Ensure HTTP errors are mapped cleanly to contract statuses."""
    # 404 -> not_available
    c_404 = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    roles, ev, status = c_404.fetch_roles("123456789")
    assert status == "not_available"
    assert roles == []

    # 429 -> blocked
    c_429 = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    roles, ev, status = c_429.fetch_roles("123456789")
    assert status == "blocked"
    assert roles == []

    # 500 -> failed
    c_500 = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    roles, ev, status = c_500.fetch_roles("123456789")
    assert status == "failed"
    assert roles == []


# ---------------------------------------------------------------------------
# Phase 4: Regnskapsregisteret Financial Accounts Tests
# ---------------------------------------------------------------------------


def test_fetch_financials_bouvet_fixture():
    """Verify live fixture extraction for Bouvet ASA accounts (org nr 974442167)."""
    fixture_path = FIXTURES_DIR / "fixtures_accounts_974442167.json"
    raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "974442167" in str(request.url):
            return httpx.Response(200, json=raw_data)
        return httpx.Response(404)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    fin, evidence, status = client.fetch_financials("974442167")

    assert status == "completed"
    assert fin is not None
    assert fin.currency == "NOK"
    assert fin.financial_year == "2025"
    assert fin.revenue == 3912343000.0
    assert fin.annual_result is not None
    assert evidence is not None
    assert evidence.source_name == "Regnskapsregisteret (Annual Accounts)"


def test_fetch_financials_equinor_fixture():
    """Verify live fixture extraction for Equinor ASA accounts (USD reporting currency)."""
    fixture_path = FIXTURES_DIR / "fixtures_accounts_923609016.json"
    raw_data = json.loads(fixture_path.read_text(encoding="utf-8"))

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "923609016" in str(request.url):
            return httpx.Response(200, json=raw_data)
        return httpx.Response(404)

    client = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    fin, evidence, status = client.fetch_financials("923609016")

    assert status == "completed"
    assert fin is not None
    assert fin.currency == "USD"
    assert fin.financial_year == "2025"
    assert fin.revenue == 106462000000.0
    assert fin.annual_result == 5058000000.0


def test_fetch_financials_error_handling():
    """Verify 404, 429, and 500 error mapping for financial accounts."""
    # 404 -> not_available
    c_404 = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    fin, ev, status = c_404.fetch_financials("123456789")
    assert status == "not_available"
    assert fin is None

    # 429 -> blocked
    c_429 = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    fin, ev, status = c_429.fetch_financials("123456789")
    assert status == "blocked"
    assert fin is None

    # 500 -> not_available (e.g. DNB Bank filing with Finanstilsynet)
    c_500 = BrregClient(http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    fin, ev, status = c_500.fetch_financials("123456789")
    assert status == "not_available"
    assert fin is None


# ---------------------------------------------------------------------------
# Phase 2: OpenGraph & Metadata Extraction Tests
# ---------------------------------------------------------------------------


def test_extract_meta_tag_both_orders():
    """Verify meta tag extraction handles both attribute orders."""
    # Order 1: property before content
    html1 = '<meta property="og:title" content="Tittel En">'
    assert NewsVerifier.extract_meta_tag(html1, "og:title") == "Tittel En"

    # Order 2: content before property
    html2 = '<meta content="Tittel To" property="og:title">'
    assert NewsVerifier.extract_meta_tag(html2, "og:title") == "Tittel To"

    # name before content
    html3 = '<meta name="publish_date" content="2026-04-10">'
    assert NewsVerifier.extract_meta_tag(html3, "publish_date") == "2026-04-10"

    # content before name
    html4 = '<meta content="2026-04-10" name="publish_date">'
    assert NewsVerifier.extract_meta_tag(html4, "publish_date") == "2026-04-10"


def test_extract_from_html_opengraph_recovery():
    """Verify OpenGraph recovery extracts article when CMS reverses attribute order."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
      <meta content="article" property="og:type">
      <meta content="Equinor vinner ny fornybarlisens i Nordsjøen" property="og:title">
      <meta content="2026-09-18T08:00:00Z" property="article:published_time">
      <link rel="canonical" href="https://www.equinor.com/news/2026-09-18-fornybarlisens">
    </head>
    <body>
      <h1>Equinor vinner ny fornybarlisens</h1>
    </body>
    </html>
    """
    nv = NewsVerifier()
    articles = nv.extract_from_html(html, "https://www.equinor.com", "equinor.com")

    assert len(articles) == 1
    art = articles[0]
    assert art.title == "Equinor vinner ny fornybarlisens i Nordsjøen"
    assert art.publication_date == "2026-09-18"
    assert art.article_url == "https://www.equinor.com/news/2026-09-18-fornybarlisens"


# ---------------------------------------------------------------------------
# Phase 3: Career Page Schema.org Job Fallback Tests
# ---------------------------------------------------------------------------


def test_discover_career_vacancies_schema_org():
    """Verify Schema.org JobPosting fallback extracts active vacancies and rejects mismatched/expired."""
    future_date = "2026-12-31"
    past_date = "2025-01-01"

    career_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <script type="application/ld+json">
      [
        {{
          "@type": "JobPosting",
          "title": "Senior Cloud Arkitekt",
          "hiringOrganization": {{"@type": "Organization", "name": "Bouvet ASA"}},
          "url": "https://www.bouvet.no/karriere/senior-cloud-arkitekt",
          "datePosted": "2026-08-01",
          "validThrough": "{future_date}",
          "jobLocation": {{"@type": "Place", "address": {{"addressLocality": "Oslo"}}}}
        }},
        {{
          "@type": "JobPosting",
          "title": "Utløpt Stilling",
          "hiringOrganization": {{"@type": "Organization", "name": "Bouvet ASA"}},
          "url": "https://www.bouvet.no/karriere/utlopt",
          "datePosted": "2024-01-01",
          "validThrough": "{past_date}"
        }},
        {{
          "@type": "JobPosting",
          "title": "Unrelated Stilling",
          "hiringOrganization": {{"@type": "Organization", "name": "Helt Annet Selskap AS"}},
          "url": "https://www.bouvet.no/karriere/annet",
          "datePosted": "2026-08-01",
          "validThrough": "{future_date}"
        }}
      ]
      </script>
    </head>
    <body><h1>Karriere</h1></body>
    </html>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/karriere" in str(request.url):
            return httpx.Response(200, text=career_html)
        return httpx.Response(404)

    jv = JobVerifier(http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)))
    jobs = jv.discover_career_vacancies(
        website_url="https://www.bouvet.no",
        legal_name="Bouvet ASA",
        org_nr="974442167",
    )

    assert jobs is not None
    assert jobs.status == "completed"
    assert len(jobs) == 1  # Only Senior Cloud Arkitekt accepted!
    assert jobs[0].job_title == "Senior Cloud Arkitekt"
    assert jobs[0].employer_name == "Bouvet ASA"
    assert jobs[0].deadline_date == future_date


# ---------------------------------------------------------------------------
# Phase 5: Terminal Envelope V2.5 Claims & Contract Compliance Tests
# ---------------------------------------------------------------------------


def test_build_terminal_envelope_v2_5_claims():
    """Verify terminal envelope includes all V2.5 claims and evidence."""
    now = utc_now()
    evidence = SourceEvidence(
        source_name="Brønnøysund Register Centre (Enhetsregisteret)",
        source_url="https://data.brreg.no/enhetsregisteret/api/enheter/974442167",
        retrieved_at=now,
        response_hash="hash123",
        status_code=200,
    )

    profile = CompanyProfile(
        org_nr="974442167",
        name="BOUVET ASA",
        management_roles=[
            CompanyRole(
                role_code="DAGL",
                role_title="Daglig leder",
                name="Per Gunnar Tronsli",
                is_active=True,
            ),
            CompanyRole(
                role_code="LEDE",
                role_title="Styrets leder",
                name="Pål Egil Rønn",
                is_active=True,
            ),
        ],
        financial_accounts=FinancialAccounts(
            financial_year="2025",
            period_end="2025-12-31",
            currency="NOK",
            revenue=3912343000.0,
            annual_result=383442000.0,
        ),
    )

    envelope = build_terminal_envelope(
        org_nr="974442167",
        run_id="v2-5-test",
        started_at=now,
        completed_at=now,
        profile=profile,
        evidence=evidence,
    )

    claims_map = {c["field"]: c for c in envelope["claims"]}
    ev_ids = {e["id"] for e in envelope["evidence"]}

    # Management roles
    assert "management_roles" in claims_map
    assert claims_map["management_roles"]["availability"] == "available"
    assert len(claims_map["management_roles"]["value"]) == 2

    assert "daglig_leder" in claims_map
    assert claims_map["daglig_leder"]["value"] == "Per Gunnar Tronsli"
    assert claims_map["daglig_leder"]["availability"] == "available"

    assert "styreleder" in claims_map
    assert claims_map["styreleder"]["value"] == "Pål Egil Rønn"
    assert claims_map["styreleder"]["availability"] == "available"

    # Financial accounts
    assert "financial_accounts" in claims_map
    assert claims_map["financial_accounts"]["availability"] == "available"
    assert claims_map["financial_accounts"]["value"]["financial_year"] == "2025"

    assert "annual_revenue" in claims_map
    assert claims_map["annual_revenue"]["value"] == 3912343000.0

    assert "annual_net_result" in claims_map
    assert claims_map["annual_net_result"]["value"] == 383442000.0

    # Verify all claim evidence_ids are resolvable in envelope['evidence']
    for c in envelope["claims"]:
        for eid in c["evidence_ids"]:
            assert eid in ev_ids

    # Verify summary syntax (no "vacancyies")
    summary = profile.generate_summary()
    assert "vacancyies" not in summary
    assert "Per Gunnar Tronsli (Daglig leder/CEO)" in summary
    assert "Pål Egil Rønn (Styreleder/Board Chair)" in summary
    assert "Financial highlights (2025): revenue 3,912,343,000 NOK; annual net result 383,442,000 NOK" in summary
