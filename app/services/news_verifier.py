"""Service for discovering, validating, and verifying genuine company news articles."""

from datetime import datetime, timezone
import hashlib
import html as html_lib
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
    "finn butikk",
    "alle butikker",
    "alle butikker og åpningstider",
    "butikker og åpningstider",
    "åpningstider",
    "om oss",
    "om selskapet",
    "kontakt oss",
    "kundeservice",
    "vare butikker",
}

# Generic path patterns that denote category pages rather than individual articles
CATEGORY_URL_PATTERNS = [
    r"^/(?:news|nyheter|category|arkiv|archive|presse|press)/?$",
    r"^/(?:news|nyheter|arkiv)/page/\d+/?$",
    r"^/(?:produkter|products|tjenester|services)/?$",
    r"^/(?:finn-butikk|butikker|stores|locations|apningstider|vare-butikker)/?.*",
    r"^/(?:om-oss|om-bouvet|about-us|kontakt|kontakt-oss|contact|kundeservice|personvern|cookies)/?.*",
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
    def is_homepage_url(url: str, base_url: str = "") -> bool:
        """Check if URL represents a website homepage or top-level landing page."""
        try:
            parsed = urlparse(url)
            path = parsed.path.strip("/")
            # Empty path or common language landing paths (e.g. /en, /no, /nb)
            if not path or path.lower() in {
                "en",
                "no",
                "nb",
                "privat",
                "bedrift",
                "home",
                "forside",
                "no/forside",
                "en/home",
                "index.html",
                "default.aspx",
            }:
                return True
            if base_url:
                base_parsed = urlparse(base_url)
                if parsed.netloc.lower() == base_parsed.netloc.lower() and path == base_parsed.path.strip("/"):
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def find_rss_feed_urls(html: str, base_url: str) -> list[str]:
        """Discover corporate RSS/Atom feed links from HTML header or body."""
        feed_pattern = re.compile(
            r'<link[^>]+(?:application/rss\+xml|application/atom\+xml)[^>]+href=[\'"]([^\'"]+)[\'"]',
            re.IGNORECASE,
        )
        found = []
        base_domain = urlparse(base_url).netloc.lower()
        if base_domain.startswith("www."):
            base_domain = base_domain[4:]

        for match in feed_pattern.findall(html):
            full = urljoin(base_url, match.strip())
            parsed = urlparse(full)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host == base_domain:
                if "/comments" in full.lower():
                    continue
                if full not in found:
                    found.append(full)
        return found

    @staticmethod
    def find_news_section_url(html: str, base_url: str) -> Optional[str]:
        """Find the primary news or press releases section link from homepage HTML."""
        news_link_pattern = re.compile(
            r'href=[\'"]([^\'"]*(?:/nyheter|/news|/presse|/aktuelt|/press-releases|/media/news)[^\'"]*)[\'"]',
            re.IGNORECASE,
        )
        matches = news_link_pattern.findall(html)
        base_domain = urlparse(base_url).netloc.lower()
        if base_domain.startswith("www."):
            base_domain = base_domain[4:]
        for m in matches:
            full_url = urljoin(base_url, m.strip())
            parsed = urlparse(full_url)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host == base_domain:
                if not any(token in full_url.lower() for token in ["login", "search", "#"]):
                    return full_url
        return None

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

            title = html_lib.unescape((title_el.text or "").strip()) if title_el is not None else ""
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

    def extract_from_html(self, html: str, base_url: str, source_domain: Optional[str] = None) -> list[NewsArticle]:
        """
        Extract articles from HTML structured data (JSON-LD NewsArticle / OpenGraph metadata).
        Strictly distinguishes datePublished from dateModified or retrieval timestamps.
        """
        articles: list[NewsArticle] = []
        seen_titles: set[str] = set()
        effective_source = source_domain or urlparse(base_url).netloc

        # 1. Search for JSON-LD structured data
        json_ld_blocks = re.findall(r'<script[^>]+type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        for block in json_ld_blocks:
            try:
                data = json.loads(block.strip())
                items = data if isinstance(data, list) else [data]
                for item in items:
                    item_type = item.get("@type", "")
                    if item_type in ("NewsArticle", "Article", "BlogPosting", "PressRelease"):
                        title = html_lib.unescape(str(item.get("headline") or item.get("name") or "").strip())
                        raw_url = str(item.get("url") or item.get("mainEntityOfPage") or base_url).strip()
                        full_url = urljoin(base_url, raw_url)
                        raw_pub = str(item.get("datePublished") or "").strip()

                        if not title or self.is_navigation_label(title):
                            continue
                        if self.is_homepage_url(full_url, base_url) or self.is_category_url(full_url):
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
                                    article_url=full_url,
                                    source=effective_source,
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
                title = html_lib.unescape(og_title_m.group(1).strip())
                raw_date = og_date_m.group(1).strip()
                raw_url = og_url_m.group(1).strip() if og_url_m else base_url
                full_url = urljoin(base_url, raw_url)
                pub_date = self.parse_iso_date(raw_date)

                if (
                    pub_date
                    and not self.is_navigation_label(title)
                    and not self.is_homepage_url(full_url, base_url)
                    and not self.is_category_url(full_url)
                ):
                    articles.append(
                        NewsArticle(
                            title=title,
                            article_url=full_url,
                            source=effective_source,
                            publication_date=pub_date,
                            retrieval_date=utc_now(),
                            evidence_span=f"Verified OpenGraph article:published_time '{pub_date}'.",
                            is_duplicate=False,
                        )
                    )

        # 3. Search semantic HTML <article> tags containing title link and <time datetime="...">
        if not articles:
            article_blocks = re.findall(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
            for block in article_blocks:
                time_m = re.search(r'<time[^>]+datetime=[\'"]([^\'"]+)[\'"]', block, re.IGNORECASE)
                link_m = re.search(r'<a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>', block, re.DOTALL | re.IGNORECASE)
                if time_m and link_m:
                    raw_pub = time_m.group(1).strip()
                    raw_url = link_m.group(1).strip()
                    raw_title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
                    raw_title = html_lib.unescape(re.sub(r'\s+', ' ', raw_title).strip())
                    pub_date = self.parse_iso_date(raw_pub)
                    full_url = urljoin(base_url, raw_url)

                    if (
                        raw_title
                        and pub_date
                        and not self.is_navigation_label(raw_title)
                        and not self.is_homepage_url(full_url, base_url)
                        and not self.is_category_url(full_url)
                    ):
                        t_hash = self.compute_title_hash(raw_title)
                        if t_hash not in seen_titles:
                            seen_titles.add(t_hash)
                            articles.append(
                                NewsArticle(
                                    title=raw_title,
                                    article_url=full_url,
                                    source=effective_source,
                                    publication_date=pub_date,
                                    retrieval_date=utc_now(),
                                    evidence_span=f"Verified semantic HTML article date '{pub_date}' for '{raw_title}'.",
                                    is_duplicate=False,
                                )
                            )

        return articles
