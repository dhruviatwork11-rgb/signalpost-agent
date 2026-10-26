"""Service for discovering, validating, and verifying genuine company news articles."""

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from app.models import NewsArticle, utc_now

# Generic navigation labels and category titles to reject
NAVIGATION_LABELS = {
    "news",
    "nyheter",
    "see all news",
    "se alle nyheter",
    "see more news",
    "se flere nyheter",
    "read more",
    "les mer",
    "press releases",
    "pressemeldinger",
    "archive",
    "arkiv",
    "overview",
    "oversikt",
    "latest news",
    "siste nytt",
    "load more",
    "next page",
    "back to overview",
    "home",
    "hjem",
    "blog",
    "blogg",
}

# Generic path patterns that denote category pages rather than individual articles
CATEGORY_URL_PATTERNS = [
    r"^/(?:news|nyheter|category|arkiv|archive|presse|press)/?$",
    r"^/(?:news|nyheter|arkiv)/page/\d+/?$",
    r"^/(?:produkter|products|tjenester|services)/?$",
]


class NewsVerifier:
    """Extracts and verifies genuine company news articles with strict publication date enforcement."""

    @staticmethod
    def normalize_title(title: str) -> str:
        """Normalize title for comparison and deduplication."""
        cleaned = re.sub(r"[^\w\s]", "", title.lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def compute_title_hash(title: str) -> str:
        """Generate SHA-256 hash of normalized title."""
        norm = NewsVerifier.normalize_title(title)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()

    @staticmethod
    def is_navigation_label(title: str) -> bool:
        """Check if title is a generic navigation element or category heading."""
        norm = NewsVerifier.normalize_title(title)
        if norm in NAVIGATION_LABELS:
            return True
        if len(norm.split()) < 3 and len(norm) < 15:
            return True
        return False

    @staticmethod
    def is_category_url(url: str) -> bool:
        """Check if URL points to a category, landing, or archive page rather than an individual article."""
        path = urlparse(url).path.lower()
        for pat in CATEGORY_URL_PATTERNS:
            if re.search(pat, path):
                return True
        return False

    @staticmethod
    def parse_iso_date(date_str: str) -> Optional[str]:
        """Validate and format date string as YYYY-MM-DD or ISO-8601 UTC."""
        clean = date_str.strip()
        # Match YYYY-MM-DD
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", clean)
        if m:
            return m.group(1)
        # Parse common RFC 2822 or ISO dates
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(clean)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    def extract_from_rss(self, rss_xml: str, source_domain: str) -> list[NewsArticle]:
        """
        Extract individual news articles from RSS/Atom feed.
        Enforces item-level publication dates and rejects feed-level/update dates.
        """
        articles: list[NewsArticle] = []
        seen_titles: set[str] = set()

        try:
            root = ET.fromstring(rss_xml)
        except Exception:
            return []

        # RSS 2.0 items
        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            pub_date_el = item.find("pubDate")

            title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            pub_date_raw = (pub_date_el.text or "").strip() if pub_date_el is not None else ""

            if not title or not link:
                continue
            if self.is_navigation_label(title) or self.is_category_url(link):
                continue

            pub_date = self.parse_iso_date(pub_date_raw) if pub_date_raw else None
            if not pub_date:
                # Reject undated items when a verified publication date is required
                continue

            title_hash = self.compute_title_hash(title)
            is_dup = title_hash in seen_titles
            seen_titles.add(title_hash)

            if not is_dup:
                articles.append(
                    NewsArticle(
                        title=title,
                        article_url=link,
                        source=source_domain,
                        publication_date=pub_date,
                        retrieval_date=utc_now(),
                        evidence_span=f"Verified RSS pubDate '{pub_date}' for article '{title}'.",
                        is_duplicate=False,
                    )
                )

        return articles

    def extract_from_html(self, html: str, base_url: str, source_domain: str) -> list[NewsArticle]:
        """
        Extract articles from HTML structured data (JSON-LD NewsArticle / OpenGraph metadata).
        Strictly distinguishes datePublished from dateModified or retrieval timestamps.
        """
        articles: list[NewsArticle] = []
        seen_titles: set[str] = set()

        # 1. Search for JSON-LD structured data
        json_ld_blocks = re.findall(r'<script[^>]+type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        for block in json_ld_blocks:
            try:
                data = json.loads(block.strip())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    item_type = item.get("@type", "")
                    if item_type in ("NewsArticle", "Article", "BlogPosting", "PressRelease"):
                        title = str(item.get("headline") or item.get("name") or "").strip()
                        url = str(item.get("url") or item.get("mainEntityOfPage") or base_url).strip()
                        raw_pub = str(item.get("datePublished") or "").strip()

                        if not title or self.is_navigation_label(title):
                            continue
                        pub_date = self.parse_iso_date(raw_pub) if raw_pub else None
                        if not pub_date:
                            continue  # Reject undated

                        t_hash = self.compute_title_hash(title)
                        if t_hash not in seen_titles:
                            seen_titles.add(t_hash)
                            articles.append(
                                NewsArticle(
                                    title=title,
                                    article_url=urljoin(base_url, url),
                                    source=source_domain,
                                    publication_date=pub_date,
                                    retrieval_date=utc_now(),
                                    evidence_span=f"Verified JSON-LD datePublished '{pub_date}' for '{title}'.",
                                    is_duplicate=False,
                                )
                            )
            except Exception:
                continue

        # 2. Search OpenGraph article metadata if no JSON-LD found
        if not articles:
            og_title_m = re.search(r'<meta[^>]+property=[\'"]og:title[\'"][^>]+content=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
            og_date_m = re.search(r'<meta[^>]+property=[\'"]article:published_time[\'"][^>]+content=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
            og_url_m = re.search(r'<meta[^>]+property=[\'"]og:url[\'"][^>]+content=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)

            if og_title_m and og_date_m:
                title = og_title_m.group(1).strip()
                raw_date = og_date_m.group(1).strip()
                url = og_url_m.group(1).strip() if og_url_m else base_url
                pub_date = self.parse_iso_date(raw_date)

                if pub_date and not self.is_navigation_label(title) and not self.is_category_url(url):
                    articles.append(
                        NewsArticle(
                            title=title,
                            article_url=url,
                            source=source_domain,
                            publication_date=pub_date,
                            retrieval_date=utc_now(),
                            evidence_span=f"Verified OpenGraph article:published_time '{pub_date}'.",
                            is_duplicate=False,
                        )
                    )

        return articles
