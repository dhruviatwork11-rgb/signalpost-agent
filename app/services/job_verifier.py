"""Service for discovering and verifying individual job advertisements from official sources."""

from datetime import datetime, timezone, timedelta
import json
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote, urlparse, urljoin
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


class JobPostingList(list):
    """List of JobPosting objects with source search metadata for contract reporting."""

    def __init__(
        self,
        items=(),
        status: str = "completed",
        search_url: str = "",
        query_term: str = "",
        error_message: Optional[str] = None,
    ):
        super().__init__(items)
        self.status = status  # completed, blocked, failed
        self.search_url = search_url
        self.query_term = query_term
        self.error_message = error_message


class JobVerifier:
    """Discovers, validates, and verifies individual job advertisements."""

    _circuit_lock = threading.Lock()
    _request_lock = threading.Lock()
    _last_request_ts = 0.0
    _min_request_interval = 1.2  # Seconds after completion before next request starts

    # Process-wide NAV circuit breaker state across all instances
    _circuit_open: bool = False
    _consecutive_429: int = 0
    _consecutive_429_threshold: int = 2
    _circuit_cooldown_seconds: float = 120.0
    _circuit_blocked_until: float = 0.0
    _probe_in_flight: bool = False

    # Observability metrics
    _nav_requests_sent: int = 0
    _nav_429_received: int = 0
    _nav_circuit_skips: int = 0

    def __init__(
        self,
        base_api_url: str = NAV_STILLINGER_SEARCH_API,
        timeout: float = 6.0,
        user_agent: str = USER_AGENT,
        http_client: Optional[httpx.Client] = None,
        db: Optional[Any] = None,
        cache_ttl_seconds: int = 86400,
        circuit_cooldown_seconds: Optional[float] = None,
        circuit_failure_threshold: Optional[int] = None,
    ):
        self.base_api_url = base_api_url
        self.timeout = timeout
        self.user_agent = user_agent
        self._http_client = http_client
        self.db = db
        self.cache_ttl_seconds = cache_ttl_seconds
        if circuit_cooldown_seconds is not None:
            JobVerifier._circuit_cooldown_seconds = float(circuit_cooldown_seconds)
        if circuit_failure_threshold is not None:
            JobVerifier._consecutive_429_threshold = int(circuit_failure_threshold)

    @classmethod
    def configure_circuit_breaker(
        cls,
        cooldown_seconds: float = 120.0,
        failure_threshold: int = 2,
    ) -> None:
        """Configure circuit breaker parameters."""
        with cls._circuit_lock:
            cls._circuit_cooldown_seconds = float(cooldown_seconds)
            cls._consecutive_429_threshold = int(failure_threshold)

    @classmethod
    def reset_circuit_breaker(cls) -> None:
        """Reset circuit breaker state to closed with zero counters."""
        with cls._circuit_lock:
            cls._circuit_open = False
            cls._consecutive_429 = 0
            cls._circuit_blocked_until = 0.0
            cls._probe_in_flight = False
            cls._nav_requests_sent = 0
            cls._nav_429_received = 0
            cls._nav_circuit_skips = 0

    @classmethod
    def get_circuit_status(cls) -> dict[str, Any]:
        """Return diagnostic metrics and state of circuit breaker."""
        with cls._circuit_lock:
            now = time.monotonic()
            is_open = cls._circuit_open
            remaining = max(0.0, cls._circuit_blocked_until - now) if is_open else 0.0
            return {
                "is_open": is_open,
                "consecutive_429": cls._consecutive_429,
                "cooldown_remaining_seconds": round(remaining, 2),
                "probe_in_flight": cls._probe_in_flight,
                "requests_sent": cls._nav_requests_sent,
                "429_received": cls._nav_429_received,
                "circuit_skips": cls._nav_circuit_skips,
            }

    @classmethod
    def _acquire_nav_permission(cls) -> tuple[bool, bool, str]:
        """
        Check circuit breaker before initiating NAV requests.
        Returns:
            (allowed: bool, is_probe: bool, reason: str)
        """
        with cls._circuit_lock:
            now = time.monotonic()
            if not cls._circuit_open:
                return True, False, ""

            # Circuit is open. Check if cooldown has expired
            if now >= cls._circuit_blocked_until:
                if not cls._probe_in_flight:
                    # Sole probe permission granted to this worker
                    cls._probe_in_flight = True
                    return True, True, "Circuit half-open: probe request in flight."
                else:
                    cls._nav_circuit_skips += 1
                    return False, False, "NAV Arbeidsplassen circuit breaker recovery probe in flight; request skipped."

            # Circuit remains strictly open
            cls._nav_circuit_skips += 1
            remaining = int(cls._circuit_blocked_until - now)
            return (
                False,
                False,
                f"NAV Arbeidsplassen circuit breaker open due to consecutive 429 rate limit responses ({remaining}s cooldown remaining).",
            )

    @classmethod
    def _record_nav_outcome(cls, status_code: Optional[int], was_probe: bool) -> None:
        """
        Update circuit breaker state based on NAV response status.
        Thread-safe under cls._circuit_lock.
        """
        with cls._circuit_lock:
            cls._nav_requests_sent += 1
            now = time.monotonic()

            if status_code == 429:
                cls._nav_429_received += 1
                cls._consecutive_429 += 1
                if was_probe or cls._consecutive_429 >= cls._consecutive_429_threshold:
                    cls._circuit_open = True
                    cls._circuit_blocked_until = now + cls._circuit_cooldown_seconds
                    cls._probe_in_flight = False
            elif status_code == 200:
                cls._consecutive_429 = 0
                cls._circuit_open = False
                cls._circuit_blocked_until = 0.0
                cls._probe_in_flight = False
            else:
                if was_probe:
                    # Probe failed to obtain 200 OK -> reopen circuit with new cooldown
                    cls._circuit_open = True
                    cls._circuit_blocked_until = now + cls._circuit_cooldown_seconds
                    cls._probe_in_flight = False

    @classmethod
    def _release_probe_if_abandoned(cls) -> None:
        """Ensure probe flag is cleaned up if a probe request failed before outcome recording."""
        with cls._circuit_lock:
            if cls._probe_in_flight:
                cls._circuit_open = True
                cls._circuit_blocked_until = time.monotonic() + cls._circuit_cooldown_seconds
                cls._probe_in_flight = False

    def _execute_request(self, url: str, headers: dict[str, str]) -> tuple[Optional[httpx.Response], Optional[str]]:
        """Pace requests to NAV Arbeidsplassen public API with dedicated inter-thread serialized rate limiting."""
        is_nav = "arbeidsplassen.nav.no" in url

        if is_nav:
            # Check circuit breaker before waiting on request lock
            with JobVerifier._circuit_lock:
                now = time.monotonic()
                if JobVerifier._circuit_open and now < JobVerifier._circuit_blocked_until and not JobVerifier._probe_in_flight:
                    JobVerifier._nav_circuit_skips += 1
                    return None, "NAV Arbeidsplassen circuit breaker open due to rate limiting."

            with JobVerifier._request_lock:
                now = time.monotonic()
                elapsed = now - JobVerifier._last_request_ts
                if elapsed < JobVerifier._min_request_interval:
                    time.sleep(JobVerifier._min_request_interval - elapsed)

                with JobVerifier._circuit_lock:
                    was_probe = JobVerifier._probe_in_flight

                response = None
                err = None
                try:
                    if self._http_client is not None:
                        response = self._http_client.get(url, headers=headers)
                    else:
                        with httpx.Client(timeout=self.timeout) as client:
                            response = client.get(url, headers=headers)
                except Exception as exc:
                    err = str(exc)
                finally:
                    JobVerifier._last_request_ts = time.monotonic()
                    status_code = response.status_code if response is not None else None
                    JobVerifier._record_nav_outcome(status_code, was_probe)

                return response, err

        try:
            if self._http_client is not None:
                response = self._http_client.get(url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.get(url, headers=headers)
            return response, None
        except Exception as exc:
            return None, str(exc)

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
        c_primary = c_tokens[0]

        # Reject if candidate has a conflicting, distinct corporate brand prefix
        # e.g., target 'Norsk Hydro' vs candidate 'Voith Hydro' (c_primary is 'voith' != 'norsk')
        if primary_brand != c_primary and c_primary not in t_tokens:
            return False

        if primary_brand in c_tokens:
            # If primary brand matches, check that candidate is not an unrelated church/club/association
            unrelated_indicators = {"kirke", "menighet", "idrettslag", "klubb", "forening", "borettslag", "pensjonistforening"}
            if any(ind in c_emp for ind in unrelated_indicators) and not any(ind in t_name for ind in unrelated_indicators):
                return False
            return True

        # Check general distinctive token overlap
        common = set(c_tokens) & set(t_tokens)
        if common - {"bygg", "invest", "data", "consulting", "service", "eiendom"}:
            return True

        return False

    @staticmethod
    def build_cache_key(org_nr: str, legal_name: str, max_results: int = 5) -> str:
        """Construct unambiguous compound cache key combining org number, normalized name, and query bounds."""
        clean_org = re.sub(r"\D", "", org_nr).strip()
        clean_name = re.sub(r"[^\w]+", "_", legal_name.lower()).strip("_")
        return f"{clean_org}:{clean_name}:max_{max_results}"

    def fetch_nav_vacancies(
        self,
        legal_name: str,
        org_nr: str,
        max_results: int = 5,
    ) -> JobPostingList:
        """
        Query NAV / Arbeidsplassen public API for individual vacancies.
        Verifies employer entity match, extracts individual posting URLs, locations,
        and application deadlines, and rejects generic career landing pages.
        Returns a JobPostingList preserving search status (completed, blocked, or failed).
        """
        clean_legal = re.sub(r"\s+", " ", legal_name).strip()
        clean_org = re.sub(r"\D", "", org_nr).strip()
        cache_key = self.build_cache_key(clean_org, clean_legal, max_results)

        # 1. Check persistent cache if database service is provided
        if self.db is not None:
            try:
                cached = self.db.get_job_search_cache(cache_key)

                # Fallback check for legacy single-key records if compound key not found
                legacy_key = clean_org
                if cached is None and legacy_key != cache_key:
                    legacy_cached = self.db.get_job_search_cache(legacy_key)
                    if legacy_cached is not None:
                        # Verify that legacy record actually belongs to this company name
                        l_name = legacy_cached.get("legal_name", "")
                        if self.verify_employer_match(l_name, clean_legal):
                            cached = legacy_cached
                        else:
                            # Invalidate mismatched legacy entry to prevent cache pollution
                            self.db.invalidate_job_search_cache(legacy_key)

                if cached is not None and cached.get("search_status") == "completed":
                    # Deep verification of all cached vacancies against target company identity
                    is_valid = True
                    cached_postings: list[JobPosting] = []
                    c_org = cached.get("org_nr", "")
                    c_name = cached.get("legal_name", "")

                    # Verify entity match between cached record and target
                    if c_org != clean_org and not self.verify_employer_match(c_name, clean_legal):
                        is_valid = False

                    if is_valid:
                        for v in cached.get("vacancies", []):
                            try:
                                posting = JobPosting.model_validate(v)
                                # Strict employer match verification for every single vacancy
                                if not self.verify_employer_match(posting.employer_name, clean_legal):
                                    is_valid = False
                                    break
                                cached_postings.append(posting)
                            except Exception:
                                is_valid = False
                                break

                    if is_valid:
                        return JobPostingList(
                            cached_postings[:max_results],
                            status="completed",
                            search_url=cached.get("search_url") or self.base_api_url,
                            query_term=cached.get("query_term") or clean_legal,
                            error_message=None,
                        )
                    else:
                        # Tainted or mismatched cache record: invalidate and purge
                        self.db.invalidate_job_search_cache(cache_key)
                        if legacy_key != cache_key:
                            self.db.invalidate_job_search_cache(legacy_key)
            except Exception:
                pass

        # 2. Check Circuit Breaker before making any external NAV HTTP calls
        allowed, is_probe, reason = self._acquire_nav_permission()
        if not allowed:
            return JobPostingList(
                [],
                status="blocked",
                search_url=self.base_api_url,
                query_term=clean_legal,
                error_message=reason,
            )

        acquired_probe = is_probe
        try:
            # Extract normalized company name without corporate form suffix
            base_name = re.sub(
                r"\b(?:ASA|AS|SF|ENK|DA|ANS|BA|FLI|NUF|IKS|KF)\b",
                "",
                clean_legal,
                flags=re.IGNORECASE,
            ).strip()
            clean_base = re.sub(r"\s+", " ", base_name).strip()

            # Prioritize search by clean base name (e.g. "Telenor", "Veidekke"), then legal name fallback
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
            search_status = "completed"
            last_search_url = ""
            effective_term = search_terms[0] if search_terms else clean_legal
            last_error = None

            for term in search_terms:
                effective_term = term
                url = f"{self.base_api_url}?q={quote(term)}&size={max_results * 2}"
                last_search_url = url

                response, err = self._execute_request(url, headers)
                if err:
                    search_status = "failed"
                    last_error = err
                    continue

                if response is None:
                    search_status = "failed"
                    continue

                if response.status_code == 429:
                    search_status = "blocked"
                    last_error = "NAV Arbeidsplassen rate-limited (HTTP 429)"
                    # Check if circuit breaker opened on this 429
                    with JobVerifier._circuit_lock:
                        circuit_is_open = JobVerifier._circuit_open
                    if circuit_is_open:
                        break

                    # Conservative retry with backoff honoring Retry-After if provided
                    backoff = 2.0
                    retry_header = response.headers.get("Retry-After")
                    if retry_header:
                        try:
                            parsed_wait = float(retry_header.strip())
                            backoff = min(max(parsed_wait, 1.0), 5.0)
                        except Exception:
                            backoff = 2.0
                    time.sleep(backoff)
                    response, err = self._execute_request(url, headers)
                    if response and response.status_code == 200:
                        search_status = "completed"
                    else:
                        search_status = "blocked"
                        last_error = err or "NAV Arbeidsplassen rate-limited on retry (HTTP 429)"
                        break

                if response.status_code != 200:
                    search_status = "failed" if response.status_code >= 500 else "blocked"
                    continue

                try:
                    payload = response.json()
                except Exception as e:
                    search_status = "failed"
                    last_error = str(e)
                    continue

                raw_hits = payload.get("hits", {})
                hit_list = raw_hits.get("hits", []) if isinstance(raw_hits, dict) else []
                for h in hit_list:
                    uuid = h.get("_source", {}).get("uuid")
                    if uuid and uuid not in seen_uuids:
                        seen_uuids.add(uuid)
                        all_hits.append(h)

                search_status = "completed"
                # Single-query optimization: if first term yielded hits, do not query the second term
                if all_hits:
                    break
        finally:
            if acquired_probe:
                JobVerifier._release_probe_if_abandoned()

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

        # Persistent cache update: only cache successfully completed searches
        if self.db is not None and search_status == "completed":
            try:
                now_dt = utc_now()
                retrieved_at_iso = now_dt.isoformat()
                expires_at_iso = (now_dt + timedelta(seconds=self.cache_ttl_seconds)).isoformat()
                vacancies_dicts = [p.model_dump(mode="json") for p in postings]
                self.db.set_job_search_cache(
                    cache_key=cache_key,
                    org_nr=clean_org,
                    legal_name=clean_legal,
                    search_status="completed",
                    vacancies=vacancies_dicts,
                    retrieved_at=retrieved_at_iso,
                    expires_at=expires_at_iso,
                    search_url=last_search_url or self.base_api_url,
                    query_term=effective_term,
                )
            except Exception:
                pass

        return JobPostingList(
            postings,
            status=search_status,
            search_url=last_search_url or self.base_api_url,
            query_term=effective_term,
            error_message=last_error,
        )

    def discover_career_vacancies(
        self,
        website_url: str,
        legal_name: str,
        org_nr: str,
        http_client: Optional[httpx.Client] = None,
        max_results: int = 10,
    ) -> Optional[JobPostingList]:
        """
        Discover individual job vacancies on company career pages via Schema.org JobPosting structured data.
        Serves as a reliable fallback when NAV Arbeidsplassen is rate-limited or blocked.
        Ensures employer matches target company and filters expired vacancies.
        """
        if not website_url:
            return None

        career_paths = ["/karriere", "/careers", "/ledige-stillinger", "/jobb"]
        client = http_client or self._http_client

        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": self.user_agent,
        }

        now_date_str = utc_now().strftime("%Y-%m-%d")

        for path in career_paths:
            cand_url = urljoin(website_url, path)
            try:
                if client is not None:
                    resp = client.get(cand_url, headers=headers)
                else:
                    with httpx.Client(timeout=4.0, follow_redirects=True) as hclient:
                        resp = hclient.get(cand_url, headers=headers)
            except Exception:
                continue

            if resp.status_code != 200 or not resp.text:
                continue

            html = resp.text[:1048576]  # Max 1MB
            json_ld_blocks = re.findall(
                r'<script[^>]+type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
                html,
                re.DOTALL | re.IGNORECASE,
            )

            candidate_postings: list[JobPosting] = []

            for block in json_ld_blocks:
                try:
                    data = json.loads(block.strip())
                except Exception:
                    continue

                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    if "@graph" in data and isinstance(data["@graph"], list):
                        items = data["@graph"]
                    else:
                        items = [data]

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("@type")
                    if item_type != "JobPosting" and (not isinstance(item_type, list) or "JobPosting" not in item_type):
                        continue

                    title = str(item.get("title") or item.get("name") or "").strip()
                    if not title or len(title) < 3:
                        continue

                    # Validate employer / hiringOrganization
                    hiring_org = item.get("hiringOrganization")
                    employer_name = None
                    if isinstance(hiring_org, dict):
                        employer_name = (hiring_org.get("name") or "").strip()
                    elif isinstance(hiring_org, str):
                        employer_name = hiring_org.strip()

                    # Verify employer match if employer name is provided
                    if employer_name:
                        if not self.verify_employer_match(employer_name, legal_name):
                            continue
                    else:
                        employer_name = legal_name

                    # Deadline / validThrough check
                    valid_through = item.get("validThrough")
                    deadline_str = str(valid_through)[:10] if valid_through else None
                    if deadline_str and deadline_str < now_date_str:
                        # Expired vacancy
                        continue

                    # Publication date / datePosted
                    date_posted = item.get("datePosted")
                    pub_date = str(date_posted)[:10] if date_posted else None

                    # Advertisement URL
                    raw_ad_url = item.get("url") or item.get("mainEntityOfPage") or cand_url
                    ad_url = urljoin(cand_url, str(raw_ad_url).strip())

                    # Location
                    loc_val = item.get("jobLocation")
                    loc_str = None
                    if isinstance(loc_val, dict):
                        addr = loc_val.get("address")
                        if isinstance(addr, dict):
                            loc_str = addr.get("addressLocality") or addr.get("streetAddress")
                        elif isinstance(addr, str):
                            loc_str = addr
                    elif isinstance(loc_val, str):
                        loc_str = loc_val

                    candidate_postings.append(
                        JobPosting(
                            job_title=title,
                            employer_name=employer_name,
                            employer_org_nr=org_nr,
                            advertisement_url=ad_url,
                            location=loc_str,
                            publication_date=pub_date,
                            deadline_date=deadline_str,
                            is_active=True,
                            evidence_span=f"Verified Schema.org JobPosting on {cand_url}: '{title}' for '{employer_name}'.",
                        )
                    )

            if candidate_postings:
                return JobPostingList(
                    candidate_postings[:max_results],
                    status="completed",
                    search_url=cand_url,
                    query_term=legal_name,
                    error_message=None,
                )

        return None
