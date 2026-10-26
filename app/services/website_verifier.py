"""Service for verifying that candidate websites belong to the exact legal entity."""

from datetime import datetime, timezone
import re
from typing import Optional
from urllib.parse import urlparse
import httpx

from app.config import REQUEST_TIMEOUT_SECONDS, USER_AGENT
from app.models import VerifiedWebsite, utc_now


class WebsiteVerifier:
    """Verifies website ownership against exact legal entity name and organisation number."""

    def __init__(
        self,
        timeout: float = 5.0,
        user_agent: str = USER_AGENT,
        http_client: Optional[httpx.Client] = None,
    ):
        self.timeout = timeout
        self.user_agent = user_agent
        self._http_client = http_client

    @staticmethod
    def normalize_url(url: str) -> str:
        """Ensure URL has an http/https scheme."""
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            return f"https://{cleaned}"
        return cleaned

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract root/registered domain from URL for comparison."""
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    def verify_website(
        self,
        candidate_url: Optional[str],
        org_nr: str,
        legal_name: str,
    ) -> VerifiedWebsite:
        """
        Verify that a candidate website belongs to the exact legal entity.
        Follows redirects safely, records final URL, and searches page content for
        the 9-digit organisation number or unambiguous legal name evidence.
        """
        if not candidate_url or not candidate_url.strip():
            return VerifiedWebsite(
                candidate_url="",
                final_url=None,
                is_verified=False,
                verification_status="not_available",
                is_parent_or_group=False,
                evidence_span="No candidate website registered for entity.",
            )

        norm_candidate = self.normalize_url(candidate_url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            if self._http_client is not None:
                response = self._http_client.get(
                    norm_candidate, headers=headers, follow_redirects=True
                )
            else:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                    response = client.get(norm_candidate, headers=headers)
        except httpx.TimeoutException:
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=None,
                is_verified=False,
                verification_status="unreachable",
                is_parent_or_group=False,
                evidence_span=f"Connection timed out after {self.timeout}s.",
            )
        except Exception as e:
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=None,
                is_verified=False,
                verification_status="unreachable",
                is_parent_or_group=False,
                evidence_span=f"Connection failed: {e}",
            )

        if response.status_code >= 400:
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=str(response.url),
                is_verified=False,
                verification_status="not_available",
                is_parent_or_group=False,
                evidence_span=f"HTTP {response.status_code} returned by candidate site.",
            )

        final_url = str(response.url)
        content_text = response.text[:524288]  # Limit inspection to first 512 KB

        # Check for 9-digit organisation number (with or without spaces: 123456789 or 123 456 789)
        spaced_org = f"{org_nr[:3]}\\s*{org_nr[3:6]}\\s*{org_nr[6:]}"
        org_pattern = re.compile(rf"\b(?:org\.?(?:nr|nummer)?[:.]?\s*)?({spaced_org})\b", re.IGNORECASE)
        org_match = org_pattern.search(content_text)

        # Check for legal name
        clean_name = re.sub(r"\s+", " ", legal_name).strip()
        name_found = clean_name.lower() in content_text.lower()

        # Check domain redirect relationship
        cand_domain = self.extract_domain(norm_candidate)
        final_domain = self.extract_domain(final_url)
        domain_changed = bool(cand_domain and final_domain and cand_domain != final_domain)

        # 1. Exact organisation number match on page (highest confidence)
        if org_match:
            span_start = max(0, org_match.start() - 30)
            span_end = min(len(content_text), org_match.end() + 30)
            snippet = re.sub(r"\s+", " ", content_text[span_start:span_end]).strip()
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=final_url,
                is_verified=True,
                verification_status="verified",
                is_parent_or_group=False,
                evidence_span=f"Found organisation number {org_nr} on page: '{snippet}'",
                html_content=content_text,
            )

        # 2. Domain changed significantly and no org number found -> Likely group/parent redirect
        if domain_changed and not org_match:
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=final_url,
                is_verified=False,
                verification_status="ambiguous",
                is_parent_or_group=True,
                evidence_span=f"Redirected from {cand_domain} to {final_domain} without target organisation number proof.",
                html_content=content_text,
            )

        # 3. Exact legal name found on own candidate domain
        if name_found:
            return VerifiedWebsite(
                candidate_url=candidate_url,
                final_url=final_url,
                is_verified=True,
                verification_status="verified",
                is_parent_or_group=False,
                evidence_span=f"Found exact legal entity name '{clean_name}' on verified domain {final_domain}.",
                html_content=content_text,
            )

        # 4. Domain reached, but neither org number nor legal name confirmed
        return VerifiedWebsite(
            candidate_url=candidate_url,
            final_url=final_url,
            is_verified=False,
            verification_status="ambiguous",
            is_parent_or_group=False,
            evidence_span=f"Website active at {final_url} but does not present verifiable ownership evidence for {legal_name} (org: {org_nr}).",
            html_content=content_text,
        )
