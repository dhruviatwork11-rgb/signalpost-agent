"""Service for discovering and independently verifying company social media profiles."""

from datetime import datetime, timezone
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from app.models import SocialProfile, utc_now

# Known social domains
SUPPORTED_PLATFORMS = {
    "linkedin": [r"linkedin\.com/company/([^/?#]+)", r"linkedin\.com/showcase/([^/?#]+)"],
    "facebook": [r"facebook\.com/(?!sharer|share|dialog|plugins|pages/|login|help)([^/?#]+)"],
    "twitter": [r"(?:twitter\.com|x\.com)/(?!intent|share|home|search)([^/?#]+)"],
    "instagram": [r"instagram\.com/(?!p/|explore/|reels/|stories/)([^/?#]+)"],
    "youtube": [r"youtube\.com/@([^/?#]+)", r"youtube\.com/c/([^/?#]+)", r"youtube\.com/channel/([^/?#]+)"],
}

# Generic template / placeholder handles
TEMPLATE_HANDLES = {
    "yourcompany",
    "your-company",
    "yourpage",
    "your_page",
    "company",
    "companyname",
    "username",
    "example",
    "profile",
    "theme",
    "template",
    "home",
    "admin",
    "share",
    "sharer",
    "intent",
}

# Third-party agency / developer accounts commonly placed in website templates
AGENCY_HANDLES = {
    "wix",
    "squarespace",
    "shopify",
    "webflow",
    "wordpress",
    "elementor",
    "maksimer",
    "synlighet",
    "coretre",
    "netlife",
    "inbound",
    "convert",
}


class SocialVerifier:
    """Discovers, validates, and independently verifies company social profiles."""

    @staticmethod
    def identify_platform(url: str) -> Optional[tuple[str, str]]:
        """Identify platform name and account handle from candidate URL."""
        cleaned = url.strip()
        for platform, patterns in SUPPORTED_PLATFORMS.items():
            for pat in patterns:
                match = re.search(pat, cleaned, re.IGNORECASE)
                if match:
                    handle = match.group(1).strip("/").strip().lower()
                    return platform, handle
        return None

    @staticmethod
    def is_share_link(url: str) -> bool:
        """Check if URL is a social sharing widget rather than a profile link."""
        lower = url.lower()
        return any(
            token in lower
            for token in [
                "sharer.php",
                "share.php",
                "intent/tweet",
                "sharearticle",
                "pin/create",
                "dialog/share",
                "share?url=",
                "intent/like",
            ]
        )

    def extract_candidate_links(self, html: str, base_url: str = "") -> list[str]:
        """Extract candidate social URLs from HTML anchor tags."""
        found: list[str] = []
        href_matches = re.findall(r'href=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
        for href in href_matches:
            full_url = urljoin(base_url, href.strip()) if base_url else href.strip()
            if self.is_share_link(full_url):
                continue
            if self.identify_platform(full_url):
                if full_url not in found:
                    found.append(full_url)
        return found

    def verify_social_profile(
        self,
        url: str,
        legal_name: str,
        org_nr: str,
        website_domain: Optional[str] = None,
    ) -> SocialProfile:
        """
        Independently verify social profile:
        - Rejects share widgets, template placeholders, agency credits, and parent group accounts.
        - Checks handle alignment with entity legal name or verified website domain.
        """
        if self.is_share_link(url):
            return SocialProfile(
                platform="unknown",
                profile_url=url,
                handle=None,
                is_verified=False,
                verification_status="rejected_template",
                evidence_span="Rejected social sharing button/widget.",
            )

        identity = self.identify_platform(url)
        if not identity:
            return SocialProfile(
                platform="unknown",
                profile_url=url,
                handle=None,
                is_verified=False,
                verification_status="rejected_unsupported",
                evidence_span="URL does not match supported company social platforms.",
            )

        platform, handle = identity

        # 1. Reject template / placeholder handles
        if handle in TEMPLATE_HANDLES or len(handle) < 2:
            return SocialProfile(
                platform=platform,
                profile_url=url,
                handle=handle,
                is_verified=False,
                verification_status="rejected_template",
                evidence_span=f"Rejected template placeholder handle '{handle}'.",
            )

        # 2. Reject web agency / developer credits
        if handle in AGENCY_HANDLES:
            return SocialProfile(
                platform=platform,
                profile_url=url,
                handle=handle,
                is_verified=False,
                verification_status="rejected_agency",
                evidence_span=f"Rejected agency developer handle '{handle}'.",
            )

        # 3. Check name and domain similarity
        clean_name = re.sub(r"[^a-zA-Z0-9]", "", legal_name).lower()
        clean_handle = re.sub(r"[^a-zA-Z0-9]", "", handle).lower()
        clean_domain = re.sub(r"[^a-zA-Z0-9]", "", (website_domain or "").split(".")[0]).lower()

        # Tokenize company name (e.g. "EQUINOR ASA" -> ["equinor", "asa"])
        name_tokens = [
            t.lower() for t in re.split(r"[\s\-_]+", legal_name)
            if len(t) > 2 and t.lower() not in {"asa", "as", "enk", "holding", "norge", "norway"}
        ]

        token_match = any(token in clean_handle for token in name_tokens) if name_tokens else False
        domain_match = bool(clean_domain and (clean_domain in clean_handle or clean_handle in clean_domain))

        if token_match or domain_match or (clean_handle in clean_name and len(clean_handle) >= 4):
            return SocialProfile(
                platform=platform,
                profile_url=url,
                final_url=url,
                handle=handle,
                is_verified=True,
                verification_status="verified",
                evidence_span=f"Verified {platform} handle '{handle}' aligns with company identity '{legal_name}'.",
            )

        # Handle belongs to a parent holding, unrelated brand, or unconfirmed entity
        return SocialProfile(
            platform=platform,
            profile_url=url,
            final_url=url,
            handle=handle,
            is_verified=False,
            verification_status="rejected_parent",
            evidence_span=f"Handle '{handle}' does not establish ownership by legal entity '{legal_name}' (org: {org_nr}).",
        )
