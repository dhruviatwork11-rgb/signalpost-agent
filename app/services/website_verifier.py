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
        is_registry_supplied: bool = True,
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

        # 4. Registry-supplied domain matching normalized legal name or distinctive brand
        if is_registry_supplied:
            # Normalize registry name peculiarities (e.g., '@' -> 'ø', '+' -> 'pluss', '(HUB)')
            s_norm = re.sub(r"\+", "pluss ", clean_name)
            s_norm = re.sub(r"@(?=[A-Za-z])", "ø", s_norm)
            s_norm = re.sub(r"^\([^)]+\)\s*", "", s_norm)
            s_norm = re.sub(r"\b(?:ASA|AS|SF|ENK|DA|ANS|BA|FLI|NUF|IKS|KF)\b", "", s_norm, flags=re.IGNORECASE).strip()
            s_norm = re.sub(r"^[\s\-_.:;!#*]+|[\s\-_.:;!#*]+$", "", s_norm).strip()

            # Check primary brand token in domain and title
            root_domain = final_domain.split(":")[0]
            generic_brand_tokens = {"group", "holding", "invest", "consulting", "service", "services", "tjenester", "eiendom", "data", "norge", "norway"}
            brand_tokens = [t for t in re.findall(r"[A-Za-z0-9øæå]+", s_norm.lower()) if len(t) >= 3 and t not in generic_brand_tokens]
            if brand_tokens and len(s_norm.split()) >= 2:
                p_brand = brand_tokens[0]
                brand_variants = {p_brand, p_brand.replace("ø", "oe"), p_brand.replace("ø", "o")}
                domain_matches_brand = any(v in root_domain for v in brand_variants)

                title_match = re.search(r"<title[^>]*>(.*?)</title>", content_text, re.IGNORECASE)
                page_title = title_match.group(1).lower() if title_match else ""
                title_or_headings_match = any(v in page_title or v in content_text[:10000].lower() for v in brand_variants)

                if domain_matches_brand and title_or_headings_match and len(p_brand) >= 5:
                    return VerifiedWebsite(
                        candidate_url=candidate_url,
                        final_url=final_url,
                        is_verified=True,
                        verification_status="verified",
                        is_parent_or_group=False,
                        evidence_span=f"Verified registry domain '{root_domain}' matching company brand '{p_brand}'.",
                        html_content=content_text,
                    )

            # 5. Check common Norwegian contact / about / investor subpages for org number or legal name
            sub_paths = ["/investor-relations", "/investor", "/om-oss", "/om", "/kontakt", "/kontakt-oss", "/about", "/contact"]
            parsed_final = urlparse(final_url)
            base_origin = f"{parsed_final.scheme}://{parsed_final.netloc}"
            for sp in sub_paths:
                sub_url = f"{base_origin}{sp}"
                try:
                    if self._http_client is not None:
                        sub_resp = self._http_client.get(sub_url, headers=headers, follow_redirects=True)
                    else:
                        with httpx.Client(timeout=self.timeout, follow_redirects=True) as sub_client:
                            sub_resp = sub_client.get(sub_url, headers=headers)
                    if sub_resp.status_code == 200 and sub_resp.text:
                        sub_text = sub_resp.text[:524288]
                        sub_org_match = org_pattern.search(sub_text)
                        if sub_org_match:
                            span_start = max(0, sub_org_match.start() - 30)
                            span_end = min(len(sub_text), sub_org_match.end() + 30)
                            snippet = re.sub(r"\s+", " ", sub_text[span_start:span_end]).strip()
                            return VerifiedWebsite(
                                candidate_url=candidate_url,
                                final_url=final_url,
                                is_verified=True,
                                verification_status="verified",
                                is_parent_or_group=False,
                                evidence_span=f"Found organisation number {org_nr} on subpage {sub_url}: '{snippet}'",
                                html_content=content_text,
                            )
                        if clean_name.lower() in sub_text.lower():
                            return VerifiedWebsite(
                                candidate_url=candidate_url,
                                final_url=final_url,
                                is_verified=True,
                                verification_status="verified",
                                is_parent_or_group=False,
                                evidence_span=f"Found exact legal entity name '{clean_name}' on subpage {sub_url}.",
                                html_content=content_text,
                            )
                except Exception:
                    continue

        # 7. Domain reached, but neither org number nor legal name confirmed -> Ambiguous
        return VerifiedWebsite(
            candidate_url=candidate_url,
            final_url=final_url,
            is_verified=False,
            verification_status="ambiguous",
            is_parent_or_group=False,
            evidence_span=f"Website active at {final_url} but does not present verifiable ownership evidence for {legal_name} (org: {org_nr}).",
            html_content=content_text,
        )

    def discover_candidate_domain(
        self,
        legal_name: str,
        org_nr: str,
    ) -> Optional[VerifiedWebsite]:
        """
        Conservative discovery path for entities whose registry website field is empty.
        Probes standard candidate domains derived from legal name and requires strong entity
        evidence (exact 9-digit org nr or exact full legal name on the page).
        """
        clean_legal = re.sub(r"\s+", " ", legal_name).strip()
        base_name = re.sub(
            r"\b(?:ASA|AS|SF|ENK|DA|ANS|BA|FLI|NUF|IKS|KF)\b",
            "",
            clean_legal,
            flags=re.IGNORECASE,
        ).strip()
        clean_token = re.sub(r"[^a-zA-Z0-9]", "", base_name).lower()
        if not clean_token or len(clean_token) < 3:
            return None

        # Build conservative candidate probe domains
        probes = [
            f"https://www.{clean_token}.no",
            f"https://{clean_token}.no",
            f"https://{clean_token}asa.com",
            f"https://www.{clean_token}.com",
        ]

        seen_hosts = set()
        for probe_url in probes:
            host = self.extract_domain(probe_url)
            if host in seen_hosts:
                continue
            seen_hosts.add(host)

            try:
                # Discovered probes require strong entity proof (exact org nr or exact legal name)
                vw = self.verify_website(
                    candidate_url=probe_url,
                    org_nr=org_nr,
                    legal_name=legal_name,
                    is_registry_supplied=False,
                )
                if vw.is_verified and vw.final_url:
                    vw.evidence_span = f"Discovered and verified domain {vw.final_url} for {legal_name}: {vw.evidence_span}"
                    return vw

                # If root page is ambiguous (reachable but lacks proof), inspect candidate sub-pages
                if (
                    vw.final_url
                    and not vw.is_verified
                    and not vw.is_parent_or_group
                    and vw.verification_status == "ambiguous"
                ):
                    parsed = urlparse(vw.final_url)
                    base_origin = f"{parsed.scheme}://{parsed.netloc}"
                    candidate_sub_paths = [
                        "/kontakt",
                        "/kontakt-oss",
                        "/investor",
                        "/en/investor-relations",
                        "/om-oss",
                        "/om-selskapet",
                    ]
                    headers = {
                        "User-Agent": self.user_agent,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    }
                    spaced_org = f"{org_nr[:3]}\\s*{org_nr[3:6]}\\s*{org_nr[6:]}"
                    org_pattern = re.compile(
                        rf"\b(?:org\.?(?:nr|nummer)?[:.]?\s*)?({spaced_org})\b",
                        re.IGNORECASE,
                    )

                    for sub_path in candidate_sub_paths:
                        sub_url = f"{base_origin}{sub_path}"
                        try:
                            if self._http_client is not None:
                                sub_resp = self._http_client.get(
                                    sub_url, headers=headers, follow_redirects=True
                                )
                            else:
                                with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                                    sub_resp = client.get(sub_url, headers=headers)
                            if sub_resp.status_code >= 400:
                                continue

                            sub_text = sub_resp.text[:524288]
                            sub_domain = self.extract_domain(str(sub_resp.url))
                            if sub_domain != host:
                                continue

                            sub_org_match = org_pattern.search(sub_text)
                            sub_name_found = clean_legal.lower() in sub_text.lower()

                            if sub_org_match:
                                span_start = max(0, sub_org_match.start() - 30)
                                span_end = min(len(sub_text), sub_org_match.end() + 30)
                                snippet = re.sub(
                                    r"\s+", " ", sub_text[span_start:span_end]
                                ).strip()
                                vw.is_verified = True
                                vw.verification_status = "verified"
                                vw.evidence_span = f"Discovered domain {vw.final_url} and verified organisation number {org_nr} on {sub_resp.url}: '{snippet}'"
                                return vw

                            if sub_name_found:
                                vw.is_verified = True
                                vw.verification_status = "verified"
                                vw.evidence_span = f"Discovered domain {vw.final_url} and verified exact legal entity name '{clean_legal}' on {sub_resp.url}."
                                return vw
                        except Exception:
                            continue
            except Exception:
                continue

        return None
