"""Service for discovering and verifying individual job advertisements from official sources."""

from datetime import datetime, timezone
import json
import re
from typing import Any, Optional
from urllib.parse import quote, urlparse
import httpx

from app.config import REQUEST_TIMEOUT_SECONDS, USER_AGENT
from app.models import JobPosting, utc_now

# Public open Arbeidsplassen endpoint (NAVs official public stillinger search API)
NAV_STILLINGER_SEARCH_API = "https://arbeidsplassen.nav.no/stillinger/api/search"
NAV_AD_BASE_URL = "https://arbeidsplassen.nav.no/stillinger/stilling"

# Generic careers landing pages to reject
GENERIC_CAREERS_PATTERNS = [
    r"^/(?:karriere|careers|jobs|stillinger|ledige-stillinger)/?$",
    r"^/(?:om-oss/karriere|about/careers)/?$",
    r".*[?&](?:q|search|query)=.*",  # Search results pages
]


class JobVerifier:
    """Discovers, validates, and verifies individual job advertisements."""

    def __init__(
        self,
        base_api_url: str = NAV_STILLINGER_SEARCH_API,
        timeout: float = 6.0,
        user_agent: str = USER_AGENT,
        http_client: Optional[httpx.Client] = None,
    ):
        self.base_api_url = base_api_url
        self.timeout = timeout
        self.user_agent = user_agent
        self._http_client = http_client

    @staticmethod
    def is_generic_careers_url(url: str) -> bool:
        """Check if URL is a generic careers landing page or search query rather than an individual job ad."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        full = f"{path}?{query}" if query else path

        # Reject search query URLs (e.g., nav.no/stillinger?q=...)
        if "arbeidsplassen.nav.no/stillinger?" in url or "nav.no/stillinger?" in url:
            return True

        for pat in GENERIC_CAREERS_PATTERNS:
            if re.search(pat, full):
                return True

        return False

    @staticmethod
    def verify_employer_match(candidate_employer: str, target_legal_name: str) -> bool:
        """Verify that the advertising employer matches the target legal company entity."""
        c_emp = re.sub(r"[^\w\s]", "", candidate_employer.lower()).strip()
        t_name = re.sub(r"[^\w\s]", "", target_legal_name.lower()).strip()

        if c_emp == t_name or c_emp in t_name or t_name in c_emp:
            return True

        # Strip standard corporate and geographic suffixes
        generic_suffixes = {"as", "asa", "avd", "norge", "norway", "holding", "group", "gruppen"}
        c_tokens = [tok for tok in c_emp.split() if tok not in generic_suffixes and len(tok) >= 3]
        t_tokens = [tok for tok in t_name.split() if tok not in generic_suffixes and len(tok) >= 3]

        if not t_tokens or not c_tokens:
            return False

        # First token of target is typically the primary brand (e.g. "Veidekke", "Telenor", "Kongsberg", "Bouvet", "Posten")
        primary_brand = t_tokens[0]
        if primary_brand in c_tokens:
            # If primary brand matches, check that candidate is not an unrelated church/club/association
            unrelated_indicators = {"kirke", "menighet", "idrettslag", "klubb", "forening", "borettslag"}
            if any(ind in c_emp for ind in unrelated_indicators) and not any(ind in t_name for ind in unrelated_indicators):
                return False
            return True

        # Check general distinctive token overlap
        common = set(c_tokens) & set(t_tokens)
        if common - {"bygg", "invest", "data", "consulting", "service", "eiendom"}:
            return True

        return False

    def fetch_nav_vacancies(
        self,
        legal_name: str,
        org_nr: str,
        max_results: int = 5,
    ) -> list[JobPosting]:
        """
        Query NAV / Arbeidsplassen public API for individual vacancies.
        Verifies employer entity match, extracts individual posting URLs, locations,
        and application deadlines, and rejects generic career landing pages.
        """
        clean_legal = re.sub(r"\s+", " ", legal_name).strip()

        # Extract normalized company name without corporate form suffix
        base_name = re.sub(
            r"\b(?:ASA|AS|SF|ENK|DA|ANS|BA|FLI|NUF|IKS|KF)\b",
            "",
            clean_legal,
            flags=re.IGNORECASE,
        ).strip()
        clean_base = re.sub(r"\s+", " ", base_name).strip()

        # Prioritize search by clean base name (e.g. "Telenor", "Veidekke"), then legal name
        search_terms = []
        if clean_base and len(clean_base) >= 2:
            search_terms.append(clean_base)
        if clean_legal not in search_terms:
            search_terms.append(clean_legal)

        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        all_hits = []
        seen_uuids = set()

        for term in search_terms:
            url = f"{self.base_api_url}?q={quote(term)}&size={max_results * 2}"
            try:
                if self._http_client is not None:
                    response = self._http_client.get(url, headers=headers)
                else:
                    with httpx.Client(timeout=self.timeout) as client:
                        response = client.get(url, headers=headers)
            except Exception:
                continue

            if response.status_code != 200:
                continue

            try:
                payload = response.json()
            except Exception:
                continue

            raw_hits = payload.get("hits", {})
            hit_list = raw_hits.get("hits", []) if isinstance(raw_hits, dict) else []
            for h in hit_list:
                uuid = h.get("_source", {}).get("uuid")
                if uuid and uuid not in seen_uuids:
                    seen_uuids.add(uuid)
                    all_hits.append(h)

            if len(all_hits) >= max_results:
                break

        postings: list[JobPosting] = []
        now_utc = utc_now()
        for hit in all_hits:
            source = hit.get("_source", {})
            uuid = source.get("uuid")
            if not uuid:
                continue

            title = source.get("title", "").strip()
            status = source.get("status", "").upper()
            if status != "ACTIVE":
                continue

            # Verify employer
            business_name = source.get("businessName") or ""
            employer_dict = source.get("employer") or {}
            employer_name = employer_dict.get("name") or business_name or ""

            if not self.verify_employer_match(employer_name, legal_name):
                # Reject parent company or unrelated employer
                continue

            # Check expiration date
            expires_str = source.get("expires")
            is_active = True
            if expires_str:
                try:
                    exp_dt = datetime.fromisoformat(expires_str)
                    if exp_dt < now_utc:
                        is_active = False
                except Exception:
                    pass

            if not is_active:
                continue

            # Extract location
            loc_list = source.get("locationList", [])
            location_parts = []
            if loc_list and isinstance(loc_list, list):
                loc0 = loc_list[0]
                city = loc0.get("city")
                municipal = loc0.get("municipal")
                if municipal:
                    location_parts.append(municipal)
                elif city:
                    location_parts.append(city)
            location_str = ", ".join(location_parts) if location_parts else None

            # Publication date & application deadline
            published = source.get("published", "")
            pub_date = published[:10] if published and len(published) >= 10 else None
            props = source.get("properties") or {}
            deadline = props.get("applicationdue") or (expires_str[:10] if expires_str else None)

            ad_url = f"{NAV_AD_BASE_URL}/{uuid}"

            postings.append(
                JobPosting(
                    job_title=title,
                    employer_name=employer_name,
                    employer_org_nr=org_nr,
                    advertisement_url=ad_url,
                    location=location_str,
                    publication_date=pub_date,
                    deadline_date=deadline,
                    is_active=True,
                    evidence_span=f"Official NAV Arbeidsplassen vacancy {uuid} for employer '{employer_name}'.",
                )
            )

            if len(postings) >= max_results:
                break

        return postings
