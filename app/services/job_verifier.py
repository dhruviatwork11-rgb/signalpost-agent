"""Service for discovering and verifying individual job advertisements from official sources."""

from datetime import datetime, timezone, timedelta
import html
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

# Expanded career paths on corporate websites (Norwegian & English)
EXPANDED_CAREER_PATHS = [
    "/karriere",
    "/careers",
    "/ledige-stillinger",
    "/jobb",
    "/om-oss/karriere",
    "/om-oss/jobb",
    "/jobb-hos-oss",
    "/arbeide-hos-oss",
    "/work-with-us",
    "/vacancies",
    "/jobs",
    "/stillinger",
]

# Supported external ATS domains
ATS_DOMAINS = [
    "teamtailor.com",
    "webcruiter.com",
    "webcruiter.no",
    "recman.no",
    "recman.io",
    "reachmee.com",
    "easycruit.com",
    "jobbnorge.no",
    "workbuster.com",
    "workbuster.se",
]

# Keywords indicating career links in navigation menus
CAREER_LINK_KEYWORDS = [
    "karriere",
    "ledige stillinger",
    "jobb",
    "careers",
    "work with us",
    "bli en av oss",
    "vacancies",
    "stillinger",
    "jobber",
    "open positions",
]


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    """Parse HTTP Retry-After header as seconds or HTTP date."""
    if not header_val:
        return None
    val = header_val.strip()
    try:
        seconds = float(val)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(val)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


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
    _min_request_interval = 2.5  # Conservative 2.5s minimum spacing between actual NAV requests

    # Process-wide NAV circuit breaker state across all instances
    _circuit_open: bool = False
    _consecutive_429: int = 0
    _consecutive_429_threshold: int = 4  # Resilient threshold (4 consecutive 429s)
    _circuit_cooldown_seconds: float = 30.0  # Base cooldown
    _circuit_blocked_until: float = 0.0
    _probe_in_flight: bool = False

    # Polite per-host spacing for corporate websites & ATS
    _host_lock = threading.Lock()
    _host_last_request_ts: dict[str, float] = {}
    _min_host_interval: float = 0.5

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
        min_request_interval: Optional[float] = None,
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
        if min_request_interval is not None:
            JobVerifier._min_request_interval = float(min_request_interval)

    @classmethod
    def configure_circuit_breaker(
        cls,
        cooldown_seconds: float = 30.0,
        failure_threshold: int = 4,
        min_request_interval: Optional[float] = None,
    ) -> None:
        """Configure circuit breaker and rate limiter parameters."""
        with cls._circuit_lock:
            cls._circuit_cooldown_seconds = float(cooldown_seconds)
            cls._consecutive_429_threshold = int(failure_threshold)
        if min_request_interval is not None:
            with cls._request_lock:
                cls._min_request_interval = float(min_request_interval)

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
        with cls._host_lock:
            cls._host_last_request_ts.clear()

    @classmethod
    def _pace_host(cls, url: str) -> None:
        """Apply polite per-host spacing to avoid flooding a corporate web server."""
        try:
            netloc = urlparse(url).netloc.lower()
            if not netloc:
                return
            with cls._host_lock:
                now = time.monotonic()
                last_ts = cls._host_last_request_ts.get(netloc, 0.0)
                elapsed = now - last_ts
                wait_time = cls._min_host_interval - elapsed
                if wait_time > 0:
                    time.sleep(wait_time)
                cls._host_last_request_ts[netloc] = time.monotonic()
        except Exception:
            pass

    @staticmethod
    def extract_title_from_slug_or_text(raw_text: str, url: str) -> str:
        """
        Extract clean job vacancy title, unescaping HTML entities and
        resolving to URL slug when anchor text is a generic call-to-action.
        """
        clean = html.unescape(raw_text or "").strip()
        clean = re.sub(r"<[^>]+>", " ", clean).strip()
        clean = re.sub(r"\s+", " ", clean).strip()

        # Normalize punctuation for generic phrase check
        normalized_phrase = re.sub(r"[.!?,;:\-]+$", "", clean).strip().lower()
        generic_phrases = {
            "les mer", "read more", "søk her", "søk på stillingen", "se alle",
            "vis mer", "ledige stillinger", "karriere", "careers", "jobs", "jobb",
            "apply", "apply now", "view job", "se utlysningen", "se utlysningen her",
            "les mer og søk her", "les mer og søk", "lese mer og søke her", "les mer her",
            "se stillingen", "søk stilling", "send søknad", "søk", "more",
        }

        generic_prefixes = (
            "les mer", "lese mer", "read more", "søk her", "søk på", "søk stilling",
            "se utlysning", "se stilling", "se alle", "vis mer", "apply", "view job",
            "send søknad", "åpne stilling", "ledige stillinger", "karriere", "careers",
        )
        is_generic = (
            not clean
            or len(clean) < 4
            or normalized_phrase in generic_phrases
            or any(normalized_phrase.startswith(p) for p in generic_prefixes)
        )

        if is_generic:
            # Fallback to extracting title from URL path / slug
            try:
                parsed = urlparse(url)
                path_parts = [p for p in parsed.path.split("/") if p]
                if path_parts:
                    slug = path_parts[-1]
                    slug = re.sub(r"\.(?:aspx|html|php|htm|shtml)$", "", slug, flags=re.IGNORECASE)
                    clean_slug = re.sub(r"^[0-9a-fA-F]{6,36}[-_]?", "", slug)
                    clean_slug = re.sub(r"^[0-9]+[-_]", "", clean_slug)
                    slug_title = re.sub(r"[-_]+", " ", clean_slug).strip()
                    if len(slug_title) >= 4 and not slug_title.isdigit():
                        return slug_title.capitalize()
            except Exception:
                pass

        return clean

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
                    # Adaptive exponential backoff for cooldown, bounded up to 120s
                    exp_factor = min(cls._consecutive_429 - cls._consecutive_429_threshold, 3) if not was_probe else 1
                    mult = 2 ** max(0, exp_factor)
                    cooldown = min(cls._circuit_cooldown_seconds * mult, 120.0)
                    cls._circuit_blocked_until = now + cooldown
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

        JobVerifier._pace_host(url)
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
    def is_vacancy_expired(deadline_str: Optional[str]) -> bool:
        """Return True if deadline_str is a valid date that is strictly in the past."""
        if not deadline_str:
            return False
        try:
            now_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            clean_date = str(deadline_str)[:10]
            return clean_date < now_date_str
        except Exception:
            return False

    @staticmethod
    def verify_employer_match(candidate_employer: str, target_legal_name: str) -> bool:
        """Verify that the advertising employer matches the target legal company entity."""
        c_emp = re.sub(r"[^\w\s]", "", candidate_employer.lower()).strip()
        t_name = re.sub(r"[^\w\s]", "", target_legal_name.lower()).strip()

        if c_emp == t_name or c_emp in t_name or t_name in c_emp:
            return True

        # Strip standard corporate, geographic, and generic business suffixes
        generic_tokens = {
            "as", "asa", "avd", "norge", "norway", "holding", "group", "gruppen",
            "oslo", "bergen", "trondheim", "stavanger", "tromsø", "kristiansand", "drammen",
            "nord", "sør", "øst", "vest", "nordic", "scandinavia",
            "bygg", "invest", "data", "consulting", "service", "services", "tjenester", "eiendom"
        }
        c_tokens = [tok for tok in c_emp.split() if tok not in {"as", "asa", "avd", "norge", "norway", "holding", "group", "gruppen"} and len(tok) >= 3]
        t_tokens = [tok for tok in t_name.split() if tok not in {"as", "asa", "avd", "norge", "norway", "holding", "group", "gruppen"} and len(tok) >= 3]

        if not t_tokens or not c_tokens:
            return False

        primary_brand = t_tokens[0]
        c_primary = c_tokens[0]

        # Distinctive tokens excluding geographic and generic terms
        distinctive_t = [t for t in t_tokens if t not in generic_tokens and not t.isdigit()]
        distinctive_c = [c for c in c_tokens if c not in generic_tokens and not c.isdigit()]

        if not distinctive_t or not distinctive_c:
            return False

        # Reject if candidate has a conflicting, distinct corporate brand prefix
        if primary_brand not in generic_tokens and c_primary not in generic_tokens:
            if primary_brand != c_primary and c_primary not in t_tokens:
                return False

        if primary_brand in c_tokens and primary_brand not in generic_tokens:
            unrelated_indicators = {"kirke", "menighet", "idrettslag", "klubb", "forening", "borettslag", "pensjonistforening"}
            if any(ind in c_emp for ind in unrelated_indicators) and not any(ind in t_name for ind in unrelated_indicators):
                return False
            return True

        # Check general distinctive token overlap
        common = set(distinctive_c) & set(distinctive_t)
        if common:
            return True

        return False

    @staticmethod
    def build_cache_key(org_nr: str, legal_name: str, max_results: int = 5) -> str:
        """Construct unambiguous compound cache key combining org number, normalized name, and query bounds."""
        clean_org = re.sub(r"\D", "", org_nr).strip()
        clean_name = re.sub(r"[^\w]+", "_", legal_name.lower()).strip("_")
        return f"{clean_org}:{clean_name}:max_{max_results}"

    @staticmethod
    def get_canonical_search_term(legal_name: str) -> str:
        """
        Extract a single canonical search term from official company legal name.
        Carefully strips standard corporate form suffixes without losing distinctive company name info.
        """
        clean_legal = re.sub(r"\s+", " ", legal_name).strip()
        # Strip standard corporate forms at word boundaries
        base = re.sub(
            r"\b(?:ASA|AS|SF|ENK|DA|ANS|BA|FLI|NUF|IKS|KF)\b",
            "",
            clean_legal,
            flags=re.IGNORECASE,
        ).strip()
        base = re.sub(r"\s+", " ", base).strip()
        # Strip leading/trailing punctuation (e.g. "- P A L M E R A -" or "----ADVOKATFIRMAET...")
        trimmed = re.sub(r"^[\s\-_.:;!#*]+|[\s\-_.:;!#*]+$", "", base).strip()
        if len(trimmed) >= 2:
            return trimmed
        if len(base) >= 2:
            return base
        return clean_legal

    def fetch_nav_vacancies(
        self,
        legal_name: str,
        org_nr: str,
        max_results: int = 5,
    ) -> JobPostingList:
        """
        Query NAV / Arbeidsplassen public API for individual vacancies.
        Uses single canonical search query per company to eliminate duplicate requests.
        Enforces process-wide rate limiting, adaptive backoff, and circuit breaker.
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
                        l_name = legacy_cached.get("legal_name", "")
                        if self.verify_employer_match(l_name, clean_legal):
                            cached = legacy_cached
                        else:
                            self.db.invalidate_job_search_cache(legacy_key)

                if cached is not None and cached.get("search_status") == "completed":
                    is_valid = True
                    cached_postings: list[JobPosting] = []
                    c_org = cached.get("org_nr", "")
                    c_name = cached.get("legal_name", "")

                    if c_org != clean_org and not self.verify_employer_match(c_name, clean_legal):
                        is_valid = False

                    if is_valid:
                        for v in cached.get("vacancies", []):
                            try:
                                posting = JobPosting.model_validate(v)
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
        canonical_term = self.get_canonical_search_term(clean_legal)
        search_url = f"{self.base_api_url}?q={quote(canonical_term)}&size={max_results * 2}"
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        all_hits = []
        search_status = "completed"
        last_error = None

        try:
            response, err = self._execute_request(search_url, headers)
            if err:
                search_status = "failed"
                last_error = err
            elif response is None:
                with JobVerifier._circuit_lock:
                    circuit_is_open = JobVerifier._circuit_open
                search_status = "blocked" if circuit_is_open else "failed"
                last_error = "Request blocked or failed before execution"
            elif response.status_code == 429:
                search_status = "blocked"
                last_error = "NAV Arbeidsplassen rate-limited (HTTP 429)"

                # Check if circuit breaker opened on this 429
                with JobVerifier._circuit_lock:
                    circuit_is_open = JobVerifier._circuit_open

                # If circuit is not open yet, attempt one bounded retry with backoff
                if not circuit_is_open:
                    backoff = 3.0
                    retry_header = response.headers.get("Retry-After")
                    parsed_retry = parse_retry_after(retry_header)
                    if parsed_retry is not None:
                        backoff = min(max(parsed_retry, 1.0), 30.0)
                    time.sleep(backoff)
                    response, err = self._execute_request(search_url, headers)
                    if response and response.status_code == 200:
                        search_status = "completed"
                        last_error = None
                    elif response and response.status_code == 429:
                        search_status = "blocked"
                        last_error = "NAV Arbeidsplassen rate-limited on retry (HTTP 429)"
                    else:
                        search_status = "failed" if (response and response.status_code >= 500) else "blocked"
                        last_error = err or f"NAV error {response.status_code if response else 'none'}"

            if response is not None and response.status_code == 200:
                try:
                    payload = response.json()
                    raw_hits = payload.get("hits", {})
                    hit_list = raw_hits.get("hits", []) if isinstance(raw_hits, dict) else []
                    seen_uuids = set()
                    for h in hit_list:
                        uuid = h.get("_source", {}).get("uuid")
                        if uuid and uuid not in seen_uuids:
                            seen_uuids.add(uuid)
                            all_hits.append(h)
                    search_status = "completed"
                except Exception as e:
                    search_status = "failed"
                    last_error = f"JSON parse error: {e}"
            elif response is not None and response.status_code != 200 and search_status != "blocked":
                search_status = "failed" if response.status_code >= 500 else "blocked"
                last_error = f"HTTP {response.status_code}"
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
                    search_url=search_url,
                    query_term=canonical_term,
                )
            except Exception:
                pass

        return JobPostingList(
            postings,
            status=search_status,
            search_url=search_url,
            query_term=canonical_term,
            error_message=last_error,
        )

    @staticmethod
    def extract_career_links_from_html(html: str, base_url: str) -> tuple[list[str], list[str]]:
        """
        Inspect HTML for career-related links.
        Returns:
            (internal_career_urls, ats_urls)
        """
        internal_urls = []
        ats_urls = []
        base_host = urlparse(base_url).netloc.lower()

        # Extract all <a href="..."> tags
        matches = re.findall(r'<a\s+[^>]*href=[\'"]([^\'"#]+)[\'"][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE)
        seen = set()

        for raw_href, raw_text in matches:
            href = raw_href.strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            text = re.sub(r"<[^>]+>", "", raw_text).strip().lower()
            full_url = urljoin(base_url, href)
            clean_url = full_url.split("#")[0].strip()
            if not clean_url or clean_url in seen:
                continue

            parsed = urlparse(clean_url)
            parsed_host = parsed.netloc.lower()

            # Check if this points to a known ATS provider
            is_ats = any(ats in parsed_host for ats in ATS_DOMAINS)
            if is_ats:
                seen.add(clean_url)
                ats_urls.append(clean_url)
                continue

            # Check if on same domain and matches career keywords
            is_same_domain = (parsed_host == base_host) or (parsed_host.endswith("." + base_host))
            if is_same_domain:
                url_path_lower = parsed.path.lower()
                matches_keyword = any(kw in text for kw in CAREER_LINK_KEYWORDS) or any(
                    kw.replace(" ", "-") in url_path_lower or kw.replace(" ", "") in url_path_lower
                    for kw in CAREER_LINK_KEYWORDS
                )
                if matches_keyword:
                    seen.add(clean_url)
                    internal_urls.append(clean_url)

        return internal_urls, ats_urls

    def discover_career_vacancies(
        self,
        website_url: str,
        legal_name: str,
        org_nr: str,
        http_client: Optional[httpx.Client] = None,
        max_results: int = 10,
        homepage_html: Optional[str] = None,
    ) -> Optional[JobPostingList]:
        """
        Discover individual job vacancies on verified company websites and linked ATS destinations.
        Uses Schema.org JobPosting structured data and semantic HTML job card extraction.
        Applies strict employer identity verification and filters expired vacancies.
        """
        if not website_url:
            return None

        client = http_client or self._http_client
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/json",
            "User-Agent": self.user_agent,
        }
        now_date_str = utc_now().strftime("%Y-%m-%d")

        # 1. Gather candidate URLs: expanded career paths + navigation links
        candidate_urls: list[str] = []
        ats_urls: list[str] = []

        # Propose standard paths
        for path in EXPANDED_CAREER_PATHS:
            cand = urljoin(website_url, path)
            if cand not in candidate_urls:
                candidate_urls.append(cand)

        # Inspect homepage HTML for career and ATS links if available
        html_to_scan = homepage_html
        if not html_to_scan:
            try:
                if client is not None:
                    h_resp = client.get(website_url, headers=headers)
                else:
                    with httpx.Client(timeout=4.0, follow_redirects=True) as hclient:
                        h_resp = hclient.get(website_url, headers=headers)
                if h_resp.status_code == 200 and h_resp.text:
                    html_to_scan = h_resp.text[:1048576]
            except Exception:
                pass

        if html_to_scan:
            extracted_career, extracted_ats = self.extract_career_links_from_html(html_to_scan, website_url)
            for u in extracted_career:
                if u not in candidate_urls:
                    candidate_urls.insert(0, u)  # Prioritize explicit navigation links
            ats_urls.extend(extracted_ats)

        # Target list to probe: top candidate URLs (max 4) + ATS URLs (max 2)
        urls_to_probe = candidate_urls[:4] + ats_urls[:2]
        discovered_postings: list[JobPosting] = []
        checked_sources = 0
        last_checked_url = website_url

        for page_url in urls_to_probe:
            JobVerifier._pace_host(page_url)
            try:
                if client is not None:
                    resp = client.get(page_url, headers=headers)
                else:
                    with httpx.Client(timeout=4.0, follow_redirects=True) as hclient:
                        resp = hclient.get(page_url, headers=headers)
            except Exception:
                continue

            if resp.status_code != 200 or not resp.text:
                continue

            checked_sources += 1
            last_checked_url = page_url
            page_html = resp.text[:1048576]  # Max 1MB

            # Check if this is an ATS URL: verify employer match before attributing
            is_ats = any(ats in urlparse(page_url).netloc.lower() for ats in ATS_DOMAINS)
            if is_ats:
                # Do not treat an ATS provider's domain alone as proof that a vacancy belongs to the target company
                ats_netloc = urlparse(page_url).netloc.lower()
                ats_title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE)
                ats_title = ats_title_match.group(1).strip() if ats_title_match else ""
                netloc_tokens = ats_netloc.split(".")
                ats_subdomain = netloc_tokens[0] if len(netloc_tokens) > 2 else ""
                is_company_ats = (
                    self.verify_employer_match(ats_subdomain, legal_name)
                    or (ats_title and self.verify_employer_match(ats_title, legal_name))
                )
                if not is_company_ats:
                    continue  # Mismatched or generic ATS page; reject

            # A. Schema.org JobPosting (JSON-LD)
            json_ld_blocks = re.findall(
                r'<script[^>]+type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
                page_html,
                re.DOTALL | re.IGNORECASE,
            )
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

                    title = html.unescape(str(item.get("title") or item.get("name") or "")).strip()
                    if not title or len(title) < 3:
                        continue

                    # Validate employer / hiringOrganization
                    hiring_org = item.get("hiringOrganization")
                    employer_name = None
                    if isinstance(hiring_org, dict):
                        employer_name = (hiring_org.get("name") or "").strip()
                    elif isinstance(hiring_org, str):
                        employer_name = hiring_org.strip()

                    if employer_name:
                        employer_name = html.unescape(employer_name).strip()
                        if not self.verify_employer_match(employer_name, legal_name):
                            continue
                    else:
                        employer_name = legal_name

                    # Deadline / validThrough check
                    valid_through = item.get("validThrough")
                    deadline_str = str(valid_through)[:10] if valid_through else None
                    if deadline_str and deadline_str < now_date_str:
                        continue  # Expired vacancy

                    pub_date = str(item.get("datePosted"))[:10] if item.get("datePosted") else None
                    raw_ad_url = item.get("url") or item.get("mainEntityOfPage") or page_url
                    ad_url = urljoin(page_url, str(raw_ad_url).strip())

                    if self.is_generic_careers_url(ad_url):
                        continue

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

                    discovered_postings.append(
                        JobPosting(
                            job_title=title,
                            employer_name=employer_name,
                            employer_org_nr=org_nr,
                            advertisement_url=ad_url,
                            location=loc_str,
                            publication_date=pub_date,
                            deadline_date=deadline_str,
                            is_active=True,
                            evidence_span=f"Verified Schema.org JobPosting on {page_url}: '{title}' for '{employer_name}'.",
                        )
                    )

            # B. Semantic HTML job card extraction (when JSON-LD is absent or partial)
            job_link_matches = re.findall(
                r'<a\s+[^>]*href=[\'"]([^\'"#]+)[\'"][^>]*>(.*?)</a>',
                page_html,
                re.DOTALL | re.IGNORECASE,
            )
            for j_href, j_raw_text in job_link_matches:
                j_url = urljoin(page_url, j_href.strip())
                if self.is_generic_careers_url(j_url):
                    continue

                is_job_dest = bool(
                    re.search(
                        r"/(?:stilling|stillinger|job|jobs|position|positions|ledig-stilling|vacancies)/[a-zA-Z0-9_\-]+",
                        j_url,
                        re.IGNORECASE,
                    )
                    or "job_id=" in j_url
                    or ("teamtailor.com/jobs/" in j_url)
                    or ("jobbnorge.no/ledige-stillinger/stilling/" in j_url)
                    or ("recman.no/job.php" in j_url or "recman.io/job/" in j_url)
                    or ("webcruiter.no" in j_url or "webcruiter.com" in j_url)
                    or ("reachmee.com" in j_url)
                    or ("easycruit.com" in j_url)
                    or ("workbuster." in j_url)
                )
                if not is_job_dest:
                    continue

                j_title = self.extract_title_from_slug_or_text(j_raw_text, j_url)
                if len(j_title) < 4:
                    continue

                # Check surrounding HTML for dates / deadline
                deadline_val = None
                deadline_match = re.search(
                    r"(?:frist|deadline|søknadsfrist)[:\s]*([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
                    page_html,
                    re.IGNORECASE,
                )
                if deadline_match:
                    d_raw = deadline_match.group(1)
                    if "-" in d_raw:
                        deadline_val = d_raw
                    elif "." in d_raw:
                        parts = d_raw.split(".")
                        if len(parts) == 3 and len(parts[2]) == 4:
                            deadline_val = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"

                if deadline_val and deadline_val < now_date_str:
                    continue  # Expired

                discovered_postings.append(
                    JobPosting(
                        job_title=j_title,
                        employer_name=legal_name,
                        employer_org_nr=org_nr,
                        advertisement_url=j_url,
                        location=None,
                        publication_date=None,
                        deadline_date=deadline_val,
                        is_active=True,
                        evidence_span=f"Verified career listing on {page_url}: '{j_title}' for '{legal_name}'.",
                    )
                )

            if discovered_postings:
                break

        # Deduplicate candidate postings
        unique_postings: list[JobPosting] = []
        seen_keys = set()
        for p in discovered_postings:
            d_key = (re.sub(r"\W+", "", p.job_title.lower()), p.deadline_date or "")
            if d_key not in seen_keys:
                seen_keys.add(d_key)
                unique_postings.append(p)

        if unique_postings:
            return JobPostingList(
                unique_postings[:max_results],
                status="completed",
                search_url=last_checked_url,
                query_term=legal_name,
                error_message=None,
            )

        if checked_sources > 0:
            return JobPostingList(
                [],
                status="completed",
                search_url=last_checked_url,
                query_term=legal_name,
                error_message=None,
            )

        return None
