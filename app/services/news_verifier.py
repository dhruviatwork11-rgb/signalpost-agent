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

# Norwegian and English month name mapping for textual publication dates
NORWEGIAN_MONTHS = {
    "januar": 1, "jan": 1, "january": 1,
    "februar": 2, "feb": 2, "february": 2,
    "mars": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "desember": 12, "des": 12, "december": 12, "dec": 12,
}


class NewsVerifier:
    """Extracts and verifies genuine company news articles with strict publication date enforcement."""

    @staticmethod
    def extract_meta_tag(html: str, property_or_name: str) -> Optional[str]:
        """
        Extract content from <meta> tag matching property, name, or itemprop attribute.
        Handles both attribute orders (content before name, or name before content).
        """
        p_esc = re.escape(property_or_name)
        # Order 1: name/property before content
        m1 = re.search(
            rf'<meta\b[^>]*?\b(?:property|name|itemprop)\s*=\s*[\'"]{p_esc}[\'"][^>]*?\bcontent\s*=\s*[\'"]([^\'"]*?)[\'"]',
            html,
            re.IGNORECASE,
        )
        if m1:
            return m1.group(1).strip()

        # Order 2: content before name/property
        m2 = re.search(
            rf'<meta\b[^>]*?\bcontent\s*=\s*[\'"]([^\'"]*?)[\'"][^>]*?\b(?:property|name|itemprop)\s*=\s*[\'"]{p_esc}[\'"]',
            html,
            re.IGNORECASE,
        )
        if m2:
            return m2.group(1).strip()

        return None

    @staticmethod
    def extract_canonical_url(html: str, base_url: str) -> Optional[str]:
        """Extract canonical URL from <link rel="canonical" href="...">."""
        m = re.search(
            r'<link\b[^>]*?\brel\s*=\s*[\'"]canonical[\'"][^>]*?\bhref\s*=\s*[\'"]([^\'"]+?)[\'"]',
            html,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<link\b[^>]*?\bhref\s*=\s*[\'"]([^\'"]+?)[\'"][^>]*?\brel\s*=\s*[\'"]canonical[\'"]',
                html,
                re.IGNORECASE,
            )
        if m:
            return urljoin(base_url, m.group(1).strip())
        return None

    @staticmethod
    def parse_norwegian_text_date(text: str) -> Optional[str]:
        """
        Parse textual, numeric Norwegian, or ISO dates into verified YYYY-MM-DD.
        Supports Norwegian textual months (e.g. '23. oktober 2026', '15. mai 2026'),
        English/international formats ('October 28, 2026', '28 October 2026'),
        and numeric formats ('23.10.2026', '2026-09-30T10:00:00Z').
        """
        if not text:
            return None
        clean = text.strip()

        # 1. ISO date: YYYY-MM-DD (with optional trailing time/timezone)
        iso_m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[T\s]|$)", clean)
        if iso_m:
            year, month, day = int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3))
            if 1990 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"

        # 2. Day-first textual month: "23. oktober 2026", "28 October 2026", "5. mai 2026", "13. okt. 2026"
        txt_m1 = re.search(r"\b(\d{1,2})\.?\s+([a-zA-ZæøåÆØÅ]{3,12})\.?\s+(\d{4})\b", clean)
        if txt_m1:
            day = int(txt_m1.group(1))
            month_word = txt_m1.group(2).lower()
            year = int(txt_m1.group(3))
            month = NORWEGIAN_MONTHS.get(month_word)
            if month and (1990 <= year <= 2030) and (1 <= day <= 31):
                return f"{year:04d}-{month:02d}-{day:02d}"

        # 3. Month-first textual format: "October 28, 2026", "Okt 28, 2026"
        txt_m2 = re.search(r"\b([a-zA-ZæøåÆØÅ]{3,12})\.?\s+(\d{1,2}),?\s+(\d{4})\b", clean)
        if txt_m2:
            month_word = txt_m2.group(1).lower()
            day = int(txt_m2.group(2))
            year = int(txt_m2.group(3))
            month = NORWEGIAN_MONTHS.get(month_word)
            if month and (1990 <= year <= 2030) and (1 <= day <= 31):
                return f"{year:04d}-{month:02d}-{day:02d}"

        # 4. Numeric Norwegian format: "23.10.2026" or "23/10/2026"
        num_m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", clean)
        if num_m:
            day = int(num_m.group(1))
            month = int(num_m.group(2))
            year = int(num_m.group(3))
            if 1990 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"

        return None

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
        # 1. Check for official hosted press rooms (e.g. MyNewsdesk for Posten Bring)
        mnd_match = re.search(r'href=[\'"](https://www\.mynewsdesk\.com/no/[^\'"]+)[\'"]', html, re.IGNORECASE)
        if mnd_match:
            candidate = mnd_match.group(1).strip()
            if not any(tok in candidate.lower() for tok in ["/images", "/contact_people", "/events", "/search"]):
                return candidate

        news_link_pattern = re.compile(
            r'href=[\'"]([^\'"]*(?:/nyheter|/news|/presse|/pressemeldinger|/aktuelt|/press-releases|/news-and-media|/media/news|/media/press-releases|/siste-nytt)[^\'"]*)[\'"]',
            re.IGNORECASE,
        )
        matches = news_link_pattern.findall(html)
        base_domain = urlparse(base_url).netloc.lower()
        if base_domain.startswith("www."):
            base_domain = base_domain[4:]

        rejected_tokens = [
            "login", "search", "#", "subscription", "subscribe", "abonner",
            "finn-butikk", "butikker", "karriere", "careers", "kontakt",
            "contact", "media-contacts", "brand-center", "media-gallery",
        ]

        valid_urls: list[str] = []
        for m in matches:
            full_url = urljoin(base_url, m.strip())
            parsed = urlparse(full_url)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host == base_domain:
                if not any(token in full_url.lower() for token in rejected_tokens):
                    valid_urls.append(full_url)

        # Prioritize dedicated archive sections over nested articles or forms
        priority_patterns = [
            r"/(?:media/news|media/press-releases|nyheter|pressemeldinger|press-releases|presse|aktuelt)/?$",
            r"/(?:en|no(?:-no)?)/global/media/news/?$",
            r"/(?:no-no|en)/media/news/?$",
        ]
        for pat in priority_patterns:
            for u in valid_urls:
                if re.search(pat, urlparse(u).path.lower()):
                    return u

        return valid_urls[0] if valid_urls else None

    @staticmethod
    def parse_iso_date(date_str: str) -> Optional[str]:
        """Validate and format date string as YYYY-MM-DD or ISO-8601 UTC."""
        clean = date_str.strip()
        # Match YYYY-MM-DD or ISO with trailing time
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", clean)
        if m:
            year, month, day = int(m.group(1)[:4]), int(m.group(1)[5:7]), int(m.group(1)[8:10])
            if 1990 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"

        # Parse common RFC 2822 or ISO dates
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(clean)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

        # Fallback to Norwegian and textual date parsing
        return NewsVerifier.parse_norwegian_text_date(clean)

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
            og_title = self.extract_meta_tag(html, "og:title") or self.extract_meta_tag(html, "twitter:title")
            raw_date = None
            date_meta_names = [
                "article:published_time",
                "og:published_time",
                "publication_date",
                "publish_date",
                "date",
                "datePublished",
                "sailthru.date",
            ]
            for d_name in date_meta_names:
                d_val = self.extract_meta_tag(html, d_name)
                if d_val:
                    raw_date = d_val
                    break

            raw_url = self.extract_canonical_url(html, base_url) or self.extract_meta_tag(html, "og:url") or base_url

            if og_title and raw_date:
                title = html_lib.unescape(og_title.strip())
                full_url = urljoin(base_url, raw_url)
                pub_date = self.parse_norwegian_text_date(raw_date)

                if (
                    pub_date
                    and not self.is_navigation_label(title)
                    and not self.is_homepage_url(full_url, base_url)
                    and not self.is_category_url(full_url)
                ):
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
                                evidence_span=f"Verified OpenGraph article metadata '{pub_date}' for '{title}'.",
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

        # 4. Search teaser/news-card components (for sites like Hydro, Orkla, Posten Bring)
        if len(articles) < 5:
            # 4A. Anchor-wrapped cards (e.g. <a href="..."><article><h2>...</h2><p class="date">...</p></article></a>)
            a_cards = re.findall(
                r'(<a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>)',
                html,
                re.DOTALL | re.IGNORECASE,
            )
            for full_a, href, inner in a_cards:
                clean_href = href.strip()
                if not clean_href or clean_href.startswith("#") or clean_href.startswith("javascript:"):
                    continue
                if any(tok in clean_href.lower() for tok in ["subscription", "subscribe", "login", "contact", "butikk"]):
                    continue

                full_url = urljoin(base_url, clean_href)
                if self.is_homepage_url(full_url, base_url) or self.is_category_url(full_url):
                    continue

                # Parse publication date from card attributes or inner tags
                date_cand = None
                data_date_m = re.search(r'data-(?:date|published|time)=[\'"]([^\'"]+)[\'"]', full_a, re.IGNORECASE)
                if data_date_m:
                    date_cand = self.parse_norwegian_text_date(data_date_m.group(1))

                if not date_cand:
                    time_m = re.search(r'<time[^>]*datetime=[\'"]([^\'"]+)[\'"][^>]*>', full_a, re.IGNORECASE)
                    if time_m:
                        date_cand = self.parse_norwegian_text_date(time_m.group(1))

                if not date_cand:
                    date_span_m = re.search(
                        r'<(?:p|span|div|time)[^>]*class=[\'"][^\'"]*(?:date|time|published|created)[^\'"]*[\'"][^>]*>(.*?)</(?:p|span|div|time)>',
                        inner,
                        re.DOTALL | re.IGNORECASE,
                    )
                    if date_span_m:
                        span_text = re.sub(r'<[^>]+>', ' ', date_span_m.group(1))
                        date_cand = self.parse_norwegian_text_date(span_text)

                if not date_cand:
                    inner_text = re.sub(r'<[^>]+>', ' ', inner)
                    date_cand = self.parse_norwegian_text_date(inner_text)

                if not date_cand:
                    continue

                # Extract title from inner heading, attribute, or text
                title_cand = None
                h_m = re.search(r'<h[1-6][^>]*>(.*?)</h[1-6]>', inner, re.DOTALL | re.IGNORECASE)
                if h_m:
                    title_cand = re.sub(r'<[^>]+>', ' ', h_m.group(1)).strip()

                if not title_cand:
                    aria_m = re.search(r'(?:aria-label|title)=[\'"]([^\'"]+)[\'"]', full_a, re.IGNORECASE)
                    if aria_m:
                        title_cand = aria_m.group(1).strip()

                if not title_cand:
                    raw_text = re.sub(r'<[^>]+>', ' ', inner).strip()
                    if len(raw_text) >= 15:
                        title_cand = raw_text

                if not title_cand:
                    continue

                title_cand = html_lib.unescape(re.sub(r'\s+', ' ', title_cand)).strip()
                if self.is_navigation_label(title_cand):
                    continue

                t_hash = self.compute_title_hash(title_cand)
                if t_hash not in seen_titles:
                    seen_titles.add(t_hash)
                    articles.append(
                        NewsArticle(
                            title=title_cand,
                            article_url=full_url,
                            source=effective_source,
                            publication_date=date_cand,
                            retrieval_date=utc_now(),
                            evidence_span=f"Verified news card date '{date_cand}' for '{title_cand}'.",
                            is_duplicate=False,
                        )
                    )

            # 4B. Container-wrapped cards (e.g. MyNewsdesk / panel stories)
            div_cards = re.findall(
                r'(<(?:div|article|li)[^>]*class=[\'"][^\'"]*(?:panel__content|cmp-teaser|teaser|card|story|news-item|article-card)[^\'"]*[\'"][^>]*>.*?</(?:div|article|li)>)',
                html,
                re.DOTALL | re.IGNORECASE,
            )
            for card in div_cards:
                a_m = re.search(r'<a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>', card, re.DOTALL | re.IGNORECASE)
                if not a_m:
                    continue
                href = a_m.group(1).strip()
                if not href or href.startswith("#") or href.startswith("javascript:"):
                    continue
                if any(tok in href.lower() for tok in ["subscription", "subscribe", "login", "contact"]):
                    continue

                full_url = urljoin(base_url, href)
                if self.is_homepage_url(full_url, base_url) or self.is_category_url(full_url):
                    continue

                # Parse date from container
                date_cand = None
                time_m = re.search(r'<time[^>]*datetime=[\'"]([^\'"]+)[\'"][^>]*>', card, re.IGNORECASE)
                if time_m:
                    date_cand = self.parse_norwegian_text_date(time_m.group(1))

                if not date_cand:
                    time_tag_m = re.search(r'<time[^>]*>(.*?)</time>', card, re.DOTALL | re.IGNORECASE)
                    if time_tag_m:
                        date_cand = self.parse_norwegian_text_date(re.sub(r'<[^>]+>', ' ', time_tag_m.group(1)))

                if not date_cand:
                    data_m = re.search(r'data-(?:date|published|time)=[\'"]([^\'"]+)[\'"]', card, re.IGNORECASE)
                    if data_m:
                        date_cand = self.parse_norwegian_text_date(data_m.group(1))

                if not date_cand:
                    date_span_m = re.search(
                        r'<(?:p|span|div)[^>]*class=[\'"][^\'"]*(?:date|time|published|created)[^\'"]*[\'"][^>]*>(.*?)</(?:p|span|div)>',
                        card,
                        re.DOTALL | re.IGNORECASE,
                    )
                    if date_span_m:
                        date_cand = self.parse_norwegian_text_date(re.sub(r'<[^>]+>', ' ', date_span_m.group(1)))

                if not date_cand:
                    continue

                # Title
                title_cand = None
                h_m = re.search(r'<h[1-6][^>]*>(.*?)</h[1-6]>', card, re.DOTALL | re.IGNORECASE)
                if h_m:
                    title_cand = re.sub(r'<[^>]+>', ' ', h_m.group(1)).strip()

                if not title_cand:
                    aria_m = re.search(r'(?:aria-label|title)=[\'"]([^\'"]+)[\'"]', a_m.group(0), re.IGNORECASE)
                    if aria_m:
                        title_cand = aria_m.group(1).strip()

                if not title_cand:
                    title_cand = re.sub(r'<[^>]+>', ' ', a_m.group(2)).strip()

                if not title_cand:
                    continue

                title_cand = html_lib.unescape(re.sub(r'\s+', ' ', title_cand)).strip()
                if self.is_navigation_label(title_cand):
                    continue

                t_hash = self.compute_title_hash(title_cand)
                if t_hash not in seen_titles:
                    seen_titles.add(t_hash)
                    articles.append(
                        NewsArticle(
                            title=title_cand,
                            article_url=full_url,
                            source=effective_source,
                            publication_date=date_cand,
                            retrieval_date=utc_now(),
                            evidence_span=f"Verified news card date '{date_cand}' for '{title_cand}'.",
                            is_duplicate=False,
                        )
                    )

        return articles
