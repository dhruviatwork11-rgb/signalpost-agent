"""Tests for V2.5.4 recall and coverage expansions:
- Robust brand and registry-supplied domain verification
- Workday ATS public CXS JSON API integration
- Subdomain career path awareness
- Vacancy title boilerplate cleaning and utility link filtering
- Independent source status semantics
"""

import pytest
import httpx
from unittest.mock import MagicMock

from app.models import CompanyProfile, VerifiedWebsite, JobPosting
from app.services.website_verifier import WebsiteVerifier
from app.services.job_verifier import JobVerifier, ATS_DOMAINS


class TestWebsiteVerifierV254:
    """Test suite for V2.5.4 website verifier enhancements."""

    def test_registry_supplied_brand_domain_match(self):
        wv = WebsiteVerifier()
        # Mock client returning HTML with title matching brand
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://www.kongsberg.com/"
        mock_resp.text = "<!DOCTYPE html><html><head><title>KONGSBERG - an international technology group</title></head><body><h1>Welcome</h1></body></html>"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        wv._http_client = mock_client

        res = wv.verify_website(
            candidate_url="www.kongsberg.com",
            org_nr="943753709",
            legal_name="KONGSBERG GRUPPEN ASA",
            is_registry_supplied=True,
        )
        assert res.is_verified is True
        assert res.verification_status == "verified"
        assert "kongsberg" in res.evidence_span.lower()

    def test_registry_supplied_norwegian_char_normalization(self):
        wv = WebsiteVerifier()
        # Test @ replaced by ø, + replaced by pluss
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://pluss2.no/"
        mock_resp.text = "<html><head><title>Pluss 2 Interiør</title></head><body><p>Velkommen til Pluss 2 Interiør</p></body></html>"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        wv._http_client = mock_client

        res = wv.verify_website(
            candidate_url="Www.pluss2.no",
            org_nr="995451697",
            legal_name="+2 INTERIØR AS",
            is_registry_supplied=True,
        )
        assert res.is_verified is True
        assert res.verification_status == "verified"

    def test_unrelated_domain_rejected(self):
        wv = WebsiteVerifier()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://www.kongsberg.com/"
        mock_resp.text = "<html><head><title>KONGSBERG</title></head><body>Welcome to Kongsberg</body></html>"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        wv._http_client = mock_client

        res = wv.verify_website(
            candidate_url="www.kongsberg.com",
            org_nr="111222333",
            legal_name="COMPLETELY UNRELATED BAKERI AS",
            is_registry_supplied=False,
        )
        assert res.is_verified is False
        assert res.verification_status == "ambiguous"


class TestJobVerifierV254:
    """Test suite for V2.5.4 job verifier recall and ATS expansions."""

    def test_expanded_ats_domains_list(self):
        expected_ats = {
            "teamtailor.com",
            "webcruiter.no",
            "recman.no",
            "easycruit.com",
            "jobbnorge.no",
            "reachmee.com",
            "myworkdayjobs.com",
            "greenhouse.io",
            "lever.co",
            "talentech.com",
        }
        for ats in expected_ats:
            assert ats in ATS_DOMAINS

    def test_clean_vacancy_title(self):
        raw_1 = "Brenner du for produktutvikling? Location: Kongsberg Defence & Aerospace read more"
        assert JobVerifier.clean_vacancy_title(raw_1) == "Brenner du for produktutvikling?"

        raw_2 = "Røkter i Harstad Arbeidssted: Harstad Søknadsfrist: 12.11.2026 Les mer og søk"
        assert JobVerifier.clean_vacancy_title(raw_2) == "Røkter i Harstad"

        raw_3 = "Senior Software Engineer | Apply now"
        assert JobVerifier.clean_vacancy_title(raw_3) == "Senior Software Engineer"

    def test_subdomain_career_link_extraction(self):
        html = """
        <html><body>
          <a href="https://careers.example.com/jobs">Ledige Stillinger</a>
          <a href="/om-oss">Om oss</a>
          <a href="https://other.com">External</a>
        </body></html>
        """
        internal, ats = JobVerifier.extract_career_links_from_html(html, "https://www.example.com")
        assert "https://careers.example.com/jobs" in internal

    def test_workday_cxs_api_extraction(self):
        jv = JobVerifier()
        workday_url = "https://testcompany.wd3.myworkdayjobs.com/TestCompany_Careers"
        legal_name = "TESTCOMPANY NORGE AS"
        org_nr = "123456789"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Lead Cloud Architect Location: Oslo read more",
                    "externalPath": "/job/Oslo/Lead-Cloud-Architect_R-100",
                    "locationsText": "Oslo, Norway",
                    "postedOn": "Posted 2 Days Ago",
                },
                {
                    "title": "Junior Developer",
                    "externalPath": "/job/Bergen/Junior-Developer_R-101",
                    "locationsText": "Bergen, Norway",
                    "postedOn": "Posted 5 Days Ago",
                },
            ],
        }

        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        jobs = jv._fetch_workday_vacancies(
            workday_url=workday_url,
            legal_name=legal_name,
            org_nr=org_nr,
            client=mock_client,
            max_results=5,
        )

        assert len(jobs) == 2
        assert jobs[0].job_title == "Lead Cloud Architect"
        assert jobs[0].advertisement_url == "https://testcompany.wd3.myworkdayjobs.com/TestCompany_Careers/job/Oslo/Lead-Cloud-Architect_R-100"
        assert jobs[0].is_active is True
        assert jobs[0].employer_name == legal_name

    def test_non_vacancy_utility_links_filtered(self):
        jv = JobVerifier()
        html = """
        <html><body>
          <a href="https://kongsberg.easycruit.com/career-center/login?iso=gb">by logging in here</a>
          <a href="https://kongsberg.easycruit.com/vacancy/12345">Senior Engineer</a>
        </body></html>
        """
        # Testing semantic extraction rejects login
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        res = jv.discover_career_vacancies(
            website_url="https://kongsberg.easycruit.com",
            legal_name="KONGSBERG GRUPPEN ASA",
            org_nr="943753709",
            http_client=mock_client,
            homepage_html=html,
        )

        assert res is not None
        urls = [j.advertisement_url for j in res]
        assert not any("login" in u for u in urls)
        assert any("vacancy/12345" in u for u in urls)
