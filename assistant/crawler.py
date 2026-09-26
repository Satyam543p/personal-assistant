"""
Advanced Web Crawler (Spider) & Headless Dynamic Browser Engine (Milestone 20)
Provides multi-level domain-bounded web crawling, cycle/infinite loop prevention,
URL canonicalization, SSRF protection, polite rate limiting, Markdown knowledge archiving,
and lazy-loaded headless Playwright Chromium rendering for dynamic JavaScript SPAs
while maintaining a strictly bounded memory footprint (< 1 GB RAM, 0 MB idle browser RAM).
"""

import abc
import asyncio
import datetime
import gc
import hashlib
import html
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field, asdict

try:
    from assistant.config import (
        WORKSPACE_ROOT,
        RESEARCH_OUTPUT_DIR,
        RESEARCH_TRUSTED_DOMAINS,
        RESEARCH_BLOCKED_DOMAINS,
    )
    from assistant.tools import Tool
except ModuleNotFoundError:
    from config import (
        WORKSPACE_ROOT,
        RESEARCH_OUTPUT_DIR,
        RESEARCH_TRUSTED_DOMAINS,
        RESEARCH_BLOCKED_DOMAINS,
    )
    from tools import Tool

logger = logging.getLogger("jarvis.crawler")


# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class CrawledPage:
    url: str
    title: str
    depth: int
    text: str
    word_count: int
    file_path: str
    links_found: int = 0
    is_dynamic: bool = False
    status_code: int = 200

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrawlJobResult:
    status: str  # "success" | "partial" | "failure"
    start_url: str
    domain: str
    pages_crawled: int
    total_words: int
    output_dir: str
    index_file: str
    duration_s: float
    pages: list[CrawledPage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pages"] = [p.to_dict() for p in self.pages]
        return d


@dataclass
class DynamicPageResult:
    status: str  # "success" | "failure"
    url: str
    title: str = ""
    text: str = ""
    links: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    duration_s: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# =====================================================================
# Abstract Interfaces (Abstractions & Interfaces First)
# =====================================================================

class DynamicBrowserFetcher(abc.ABC):
    """Abstract interface for lazy-loaded headless browser page rendering."""

    @property
    @abc.abstractmethod
    def is_active(self) -> bool:
        """Returns True if the browser engine or worker is currently active."""
        pass

    @abc.abstractmethod
    async def fetch_dynamic_page(
        self,
        url: str,
        wait_selector: str | None = None,
        take_screenshot: bool = False,
        timeout_s: float = 15.0
    ) -> DynamicPageResult:
        """Renders client-side JavaScript, extracts rendered DOM text, and extracts links."""
        pass

    @abc.abstractmethod
    async def close_browser(self):
        """Immediately closes any active browser processes to reclaim memory."""
        pass


class WebCrawler(abc.ABC):
    """Abstract interface for the deep multi-level site crawler (spider)."""

    @abc.abstractmethod
    async def crawl(
        self,
        start_url: str,
        max_depth: int = 2,
        max_pages: int = 20,
        domain_restricted: bool = True,
        output_dir: str | None = None
    ) -> CrawlJobResult:
        """Performs recursive domain-bounded crawling, saves Markdown articles, and builds INDEX.md."""
    @abc.abstractmethod
    def cancel_crawl(self, job_id: str) -> bool:
        """Cancels an active crawling job by its job_id."""
        pass


# =====================================================================
# Edge Case Filters & Sanitizers
# =====================================================================

# Prohibited binary / media extensions to prevent spider trap bloat
IGNORED_EXTENSIONS = frozenset({
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".msi", ".bin", ".iso", ".dmg",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac",
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
    ".css", ".js", ".json", ".xml", ".rss", ".atom", ".woff", ".woff2", ".ttf", ".eot"
})

TRACKING_QUERY_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "ref_src", "spm"
})


def canonicalize_url(url: str) -> str | None:
    """
    Normalizes a URL to prevent duplicate crawling:
    - Strips URL fragments (#section)
    - Strips marketing/tracking query parameters (utm_*, ref, fbclid)
    - Normalizes lowercase host
    - Strips trailing slash on path
    """
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme not in ("http", "https"):
            return None

        netloc = parsed.netloc.lower()
        if not netloc:
            return None

        # Clean query parameters
        clean_query = []
        if parsed.query:
            query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
            for k, v in query_pairs:
                if k.lower() not in TRACKING_QUERY_PARAMS:
                    clean_query.append((k, v))
        
        query_str = urllib.parse.urlencode(clean_query) if clean_query else ""
        path = parsed.path.rstrip("/") if parsed.path != "/" else "/"

        canonical = urllib.parse.urlunsplit((parsed.scheme, netloc, path, query_str, ""))
        return canonical
    except Exception:
        return None


def is_safe_and_permitted_url(url: str, start_domain: str, domain_restricted: bool = True, allow_local: bool = False) -> tuple[bool, str]:
    """
    Validates URL safety:
    - Ensures valid HTTP/HTTPS scheme
    - Rejects binary file extensions
    - Blocks SSRF / private IP subnets and loopbacks (unless allow_local=True for testing)
    - Enforces domain boundary (stays within target host)
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Prohibited scheme: {parsed.scheme}"

        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname"

        # Check binary file extension in path
        path_lower = parsed.path.lower()
        for ext in IGNORED_EXTENSIONS:
            if path_lower.endswith(ext):
                return False, f"Prohibited binary/media extension: {ext}"

        # Domain boundary check
        if domain_restricted and start_domain:
            clean_host = hostname.lower()
            if clean_host.startswith("www."):
                clean_host = clean_host[4:]
            target_host = start_domain.lower()
            if target_host.startswith("www."):
                target_host = target_host[4:]

            if clean_host != target_host and not clean_host.endswith(f".{target_host}"):
                return False, f"Domain boundary restriction: '{clean_host}' is outside '{target_host}'"

        # SSRF checks: loopback & private subnets (bypass only if allow_local=True)
        if not allow_local:
            if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
                return False, "Loopback addresses blocked for security"

            try:
                ip_str = socket.gethostbyname(hostname)
                parts = [int(p) for p in ip_str.split(".") if p.isdigit()]
                if len(parts) == 4:
                    # 127.0.0.0/8
                    if parts[0] == 127:
                        return False, "Loopback IP blocked"
                    # 10.0.0.0/8
                    if parts[0] == 10:
                        return False, "Private RFC 1918 (10.x) blocked"
                    # 172.16.0.0/12
                    if parts[0] == 172 and 16 <= parts[1] <= 31:
                        return False, "Private RFC 1918 (172.16-31.x) blocked"
                    # 192.168.0.0/16
                    if parts[0] == 192 and parts[1] == 168:
                        return False, "Private RFC 1918 (192.168.x) blocked"
                    # 169.254.0.0/16 (link-local)
                    if parts[0] == 169 and parts[1] == 254:
                        return False, "Link-local IP blocked"
            except Exception:
                pass  # DNS failure will be caught on connect

        return True, "URL permitted"
    except Exception as e:
        return False, f"URL parse error: {e}"


def sanitize_filename(name: str, max_length: int = 60) -> str:
    """Sanitizes page titles into valid Windows filenames."""
    clean = re.sub(r'[<>:"/\\|?*]', '_', name).strip()
    clean = re.sub(r'\s+', '_', clean)
    clean = clean.strip("._ ")
    return clean[:max_length] or "page"


# =====================================================================
# Concrete Playwright Dynamic Fetcher (Lazy-Loaded)
# =====================================================================

class PlaywrightDynamicFetcher(DynamicBrowserFetcher):
    """
    Lazy-loaded dynamic browser fetcher utilizing Playwright Chromium.
    Launches headless browser strictly on demand and automatically closes it
    immediately after execution to preserve the < 1 GB steady-state RAM constraint.
    """

    def __init__(self, default_timeout_s: float = 12.0):
        self.default_timeout_s = default_timeout_s
        self._is_active = False

    @property
    def is_active(self) -> bool:
        return self._is_active

    async def fetch_dynamic_page(
        self,
        url: str,
        wait_selector: str | None = None,
        take_screenshot: bool = False,
        timeout_s: float | None = None
    ) -> DynamicPageResult:
        effective_timeout = timeout_s or self.default_timeout_s
        start_t = time.time()
        self._is_active = True

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._is_active = False
            return DynamicPageResult(
                status="failure",
                url=url,
                message="Playwright is not installed. Install via 'pip install playwright' and 'playwright install chromium'."
            )

        screenshot_path = None
        links = []
        rendered_text = ""
        page_title = ""

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                try:
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                    page = await context.new_page()
                    page.set_default_timeout(effective_timeout * 1000)

                    # Navigate to URL
                    await page.goto(url, wait_until="domcontentloaded", timeout=effective_timeout * 1000)

                    if wait_selector:
                        try:
                            await page.wait_for_selector(wait_selector, timeout=5000)
                        except Exception:
                            pass
                    else:
                        # Brief wait for client-side frameworks (React/Vue) to render DOM
                        await page.wait_for_timeout(1000)

                    page_title = await page.title()

                    # Extract inner text from main content or body
                    rendered_text = await page.evaluate("""() => {
                        // Remove scripts, styles, navs, headers, footers
                        const removals = document.querySelectorAll('script, style, nav, header, footer, noscript, iframe, svg');
                        removals.forEach(el => el.remove());
                        const main = document.querySelector('main, article, [role="main"], #content, .content');
                        return main ? main.innerText : document.body.innerText;
                    }""")

                    # Extract all rendered hyperlinks
                    raw_links = await page.evaluate("""() => {
                        const anchors = Array.from(document.querySelectorAll('a[href]'));
                        return anchors.map(a => a.href);
                    }""")
                    links = list(set([canonicalize_url(l) for l in raw_links if l and canonicalize_url(l)]))

                    # Optional screenshot capture
                    if take_screenshot:
                        crawls_dir = os.path.join(RESEARCH_OUTPUT_DIR, "screenshots")
                        os.makedirs(crawls_dir, exist_ok=True)
                        h = hashlib.md5(url.encode()).hexdigest()[:8]
                        screenshot_path = os.path.join(crawls_dir, f"snap_{h}.png")
                        await page.screenshot(path=screenshot_path, full_page=False)

                finally:
                    await browser.close()
                    # Trigger garbage collection to reclaim memory immediately
                    gc.collect()

            duration = round(time.time() - start_t, 2)
            self._is_active = False

            return DynamicPageResult(
                status="success",
                url=url,
                title=page_title or "Rendered Page",
                text=rendered_text.strip(),
                links=links,
                screenshot_path=screenshot_path,
                duration_s=duration,
                message=f"Rendered dynamic page in {duration}s."
            )

        except Exception as e:
            self._is_active = False
            duration = round(time.time() - start_t, 2)
            logger.error(f"Playwright rendering failed for {url}: {e}")
            return DynamicPageResult(
                status="failure",
                url=url,
                duration_s=duration,
                message=f"Dynamic rendering failed: {e}"
            )

    async def close_browser(self):
        self._is_active = False
        gc.collect()


# =====================================================================
# Concrete Web Crawler (Spider) Implementation
# =====================================================================

class JarvisWebCrawler(WebCrawler):
    """
    Production-grade, memory-efficient web crawler supporting:
    - Multi-level recursive BFS link traversal
    - Strict domain / path bounding
    - Cycle & infinite loop protection (hash set deduplication, max_depth, max_pages)
    - URL canonicalization & binary extension filtering
    - SSRF defense
    - Polite rate limiting (150-300ms delay between pages)
    - Automatic dynamic SPA detection & Playwright escalation
    - Clean Markdown archiving & master INDEX.md generation
    """

    MAX_CRAWL_PAGES_LIMIT = 50
    MAX_CRAWL_DEPTH_LIMIT = 4

    def __init__(
        self,
        dynamic_fetcher: DynamicBrowserFetcher | None = None,
        default_output_dir: str | None = None,
        page_delay_s: float = 0.2,
        allow_local: bool = False
    ):
        self.dynamic_fetcher = dynamic_fetcher or PlaywrightDynamicFetcher()
        self.default_output_dir = default_output_dir or os.path.join(RESEARCH_OUTPUT_DIR, "crawls")
        self.page_delay_s = page_delay_s
        self.allow_local = allow_local
        self._active_jobs: set[str] = set()

    def cancel_crawl(self, job_id: str) -> bool:
        if job_id in self._active_jobs:
            self._active_jobs.remove(job_id)
            return True
        return False

    async def crawl(
        self,
        start_url: str,
        max_depth: int = 2,
        max_pages: int = 15,
        domain_restricted: bool = True,
        output_dir: str | None = None
    ) -> CrawlJobResult:
        start_time = time.time()
        job_id = hashlib.md5(f"{start_url}_{start_time}".encode()).hexdigest()[:8]
        self._active_jobs.add(job_id)

        # Enforce hard safety bounds
        effective_depth = min(self.MAX_CRAWL_DEPTH_LIMIT, max(1, int(max_depth)))
        effective_pages = min(self.MAX_CRAWL_PAGES_LIMIT, max(1, int(max_pages)))

        canon_start = canonicalize_url(start_url)
        if not canon_start:
            return CrawlJobResult(
                status="failure",
                start_url=start_url,
                domain="",
                pages_crawled=0,
                total_words=0,
                output_dir="",
                index_file="",
                duration_s=0.0,
                message=f"Invalid or unsupported start URL: '{start_url}'"
            )

        parsed_start = urllib.parse.urlparse(canon_start)
        start_domain = parsed_start.hostname or ""

        # Validate start URL safety
        ok, reason = is_safe_and_permitted_url(canon_start, start_domain, domain_restricted=False, allow_local=self.allow_local)
        if not ok:
            return CrawlJobResult(
                status="failure",
                start_url=canon_start,
                domain=start_domain,
                pages_crawled=0,
                total_words=0,
                output_dir="",
                index_file="",
                duration_s=0.0,
                message=f"Start URL rejected by safety policy: {reason}"
            )

        # Prepare destination folder
        clean_domain_name = sanitize_filename(start_domain)
        timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        target_dir = output_dir or os.path.join(self.default_output_dir, f"{clean_domain_name}_{timestamp_str}")
        os.makedirs(target_dir, exist_ok=True)

        visited_urls: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(canon_start, 0)])
        crawled_pages: list[CrawledPage] = []
        errors: list[str] = []
        total_word_count = 0

        logger.info(f"Starting web crawl: {canon_start} (Max Depth: {effective_depth}, Max Pages: {effective_pages})")

        while queue and len(visited_urls) < effective_pages:
            if job_id not in self._active_jobs:
                logger.info(f"Crawl job {job_id} cancelled.")
                break

            current_url, current_depth = queue.popleft()
            if current_url in visited_urls:
                continue

            visited_urls.add(current_url)

            # Polite crawl delay between pages to avoid tripping remote rate limits
            if len(visited_urls) > 1 and self.page_delay_s > 0:
                await asyncio.sleep(self.page_delay_s)

            # Fetch and parse page
            page_data, extracted_links, err = await self._fetch_and_clean_page(current_url)
            if err:
                errors.append(f"Failed {current_url}: {err}")
                continue

            if not page_data:
                continue

            # Save page to Markdown file
            page_num = len(crawled_pages) + 1
            safe_page_title = sanitize_filename(page_data["title"]) or f"page_{page_num}"
            md_filename = f"{page_num:02d}_{safe_page_title}.md"
            md_path = os.path.join(target_dir, md_filename)

            md_content = self._format_page_markdown(
                url=current_url,
                title=page_data["title"],
                depth=current_depth,
                word_count=page_data["word_count"],
                text=page_data["text"],
                is_dynamic=page_data.get("is_dynamic", False)
            )

            try:
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(md_content)
            except Exception as e:
                errors.append(f"File write error for {md_path}: {e}")
                continue

            page_record = CrawledPage(
                url=current_url,
                title=page_data["title"],
                depth=current_depth,
                text=page_data["text"],
                word_count=page_data["word_count"],
                file_path=md_path,
                links_found=len(extracted_links),
                is_dynamic=page_data.get("is_dynamic", False)
            )
            crawled_pages.append(page_record)
            total_word_count += page_data["word_count"]

            logger.info(f"[{len(crawled_pages)}/{effective_pages}] (Depth {current_depth}) Crawled: {current_url} ({page_data['word_count']} words)")

            # If under depth limit, enqueue safe child links
            if current_depth < effective_depth and len(visited_urls) < effective_pages:
                for link in extracted_links:
                    canon_link = canonicalize_url(link)
                    if not canon_link or canon_link in visited_urls:
                        continue

                    safe_ok, _ = is_safe_and_permitted_url(canon_link, start_domain, domain_restricted, allow_local=self.allow_local)
                    if safe_ok and not any(canon_link == item[0] for item in queue):
                        queue.append((canon_link, current_depth + 1))

        # Generate Master INDEX.md
        index_path = os.path.join(target_dir, "INDEX.md")
        index_content = self._generate_master_index(
            start_url=canon_start,
            domain=start_domain,
            crawled_pages=crawled_pages,
            max_depth=effective_depth,
            max_pages=effective_pages,
            duration_s=round(time.time() - start_time, 2),
            errors=errors
        )
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(index_content)
        except Exception as e:
            errors.append(f"Failed to generate INDEX.md: {e}")

        duration = round(time.time() - start_time, 2)
        self._active_jobs.discard(job_id)

        status_result = "success" if crawled_pages else ("partial" if errors else "failure")
        msg = f"Crawled {len(crawled_pages)} pages from '{start_domain}' in {duration}s. Knowledge index saved to '{os.path.basename(index_path)}'."

        return CrawlJobResult(
            status=status_result,
            start_url=canon_start,
            domain=start_domain,
            pages_crawled=len(crawled_pages),
            total_words=total_word_count,
            output_dir=target_dir,
            index_file=index_path,
            duration_s=duration,
            pages=crawled_pages,
            errors=errors,
            message=msg
        )

    async def _fetch_and_clean_page(self, url: str) -> tuple[dict | None, list[str], str | None]:
        """
        Retrieves page via fast HTTP first. If client-side SPA detected or dynamic JS required,
        automatically escalates to Playwright Chromium.
        """
        loop = asyncio.get_event_loop()
        try:
            raw_html = await loop.run_in_executor(None, self._http_get, url)
        except Exception as e:
            return None, [], str(e)

        # Detect SPA or empty JavaScript container
        is_spa = self._is_spa_container(raw_html)

        if is_spa and self.dynamic_fetcher:
            logger.info(f"Detected dynamic JavaScript SPA for {url}. Escalating to Playwright...")
            dyn_res = await self.dynamic_fetcher.fetch_dynamic_page(url, timeout_s=12.0)
            if dyn_res.status == "success" and dyn_res.text:
                return {
                    "title": dyn_res.title,
                    "text": dyn_res.text,
                    "word_count": len(dyn_res.text.split()),
                    "is_dynamic": True
                }, dyn_res.links, None

        # Standard HTML parsing
        title, text, links = self._parse_html(url, raw_html)
        return {
            "title": title,
            "text": text,
            "word_count": len(text.split()),
            "is_dynamic": False
        }, links, None

    def _http_get(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type and "text/plain" not in content_type and "application/xhtml" not in content_type:
                raise ValueError(f"Non-HTML content type: {content_type}")
            # Limit page download to 150 KB to preserve memory
            raw_bytes = resp.read(150 * 1024)
            return raw_bytes.decode("utf-8", errors="replace")

    def _is_spa_container(self, raw_html: str) -> bool:
        """Checks if page is a React/Vue/Angular client-side container with no pre-rendered text."""
        lower = raw_html.lower()
        has_root = ('id="root"' in lower or 'id="app"' in lower or 'id="__next"' in lower)
        has_noscript = '<noscript>' in lower
        # If HTML has SPA tags and very short text body, it requires JS rendering
        clean_sample = re.sub(r'<[^>]+>', ' ', raw_html).strip()
        return (has_root or has_noscript) and len(clean_sample) < 300

    def _parse_html(self, base_url: str, html_content: str) -> tuple[str, str, list[str]]:
        # Extract title
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
        raw_title = title_match.group(1).strip() if title_match else "Untitled"
        title = html.unescape(re.sub(r'\s+', ' ', raw_title))

        # Extract links before stripping tags
        link_pat = re.compile(r'<a[^>]+href=["\']([^"\']+)["\']', re.IGNORECASE)
        found_links = []
        for m in link_pat.finditer(html_content):
            raw_href = m.group(1).strip()
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            abs_url = urllib.parse.urljoin(base_url, raw_href)
            canon = canonicalize_url(abs_url)
            if canon:
                found_links.append(canon)

        # Remove boilerplates (<script>, <style>, <nav>, <header>, <footer>, <aside>, modal)
        cleaned = re.sub(r'<(script|style|nav|header|footer|aside|svg|iframe)[^>]*>.*?</\1>', ' ', html_content, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r'<!--.*?-->', ' ', cleaned, flags=re.DOTALL)

        # Convert block tags to newlines
        cleaned = re.sub(r'</?(h[1-6]|p|div|li|tr|br|blockquote)[^>]*>', '\n', cleaned, flags=re.IGNORECASE)
        # Strip all remaining tags
        cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
        cleaned = html.unescape(cleaned)

        # Normalize whitespace while preserving paragraphs
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        text = "\n\n".join(lines)

        return title, text, list(set(found_links))

    def _format_page_markdown(self, url: str, title: str, depth: int, word_count: int, text: str, is_dynamic: bool) -> str:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        engine_str = "Headless Browser (Playwright)" if is_dynamic else "HTTP Static Scraper"
        return f"""---
title: "{title.replace('"', '')}"
source_url: "{url}"
crawl_depth: {depth}
word_count: {word_count}
engine: "{engine_str}"
crawled_at: "{date_str}"
---

# {title}

**Source:** [{url}]({url})  
**Crawl Depth:** Level {depth} | **Length:** {word_count} words | **Engine:** {engine_str}

---

{text}
"""

    def _generate_master_index(
        self,
        start_url: str,
        domain: str,
        crawled_pages: list[CrawledPage],
        max_depth: int,
        max_pages: int,
        duration_s: float,
        errors: list[str]
    ) -> str:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_words = sum(p.word_count for p in crawled_pages)

        lines = [
            f"# Site Crawl Index: {domain}",
            "",
            f"- **Start URL:** {start_url}",
            f"- **Domain:** `{domain}`",
            f"- **Pages Crawled:** {len(crawled_pages)} / {max_pages} limit",
            f"- **Maximum Depth:** Level {max_depth}",
            f"- **Total Word Count:** {total_words:,} words",
            f"- **Crawl Duration:** {duration_s} seconds",
            f"- **Generated At:** {date_str}",
            "",
            "---",
            "",
            "## Table of Contents",
            ""
        ]

        # Group pages by crawl depth
        for d in range(max_depth + 1):
            depth_pages = [p for p in crawled_pages if p.depth == d]
            if not depth_pages:
                continue
            depth_label = "Entrypoint (Root)" if d == 0 else f"Depth Level {d}"
            lines.append(f"### {depth_label}")
            lines.append("")
            for p in depth_pages:
                rel_file = os.path.basename(p.file_path)
                dyn_tag = " `[Dynamic JS]`" if p.is_dynamic else ""
                lines.append(f"- [{p.title}]({rel_file}){dyn_tag} — `{p.word_count} words` ([Source URL]({p.url}))")
            lines.append("")

        if errors:
            lines.append("---")
            lines.append("## Crawl Errors & Skipped Pages")
            lines.append("")
            for err in errors[:10]:
                lines.append(f"- ⚠ {err}")
            if len(errors) > 10:
                lines.append(f"- ...and {len(errors) - 10} more.")
            lines.append("")

        return "\n".join(lines)


# =====================================================================
# Dedicated Capability Tools
# =====================================================================

class CrawlWebsiteTool(Tool):
    """
    Crawls a documentation website or domain recursively, generates structured
    Markdown articles, and writes an INDEX.md table of contents.
    """

    def __init__(self, crawler: WebCrawler):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "start_url": {
                        "type": "string",
                        "description": "Root URL to begin crawling from."
                    },
                    "max_depth": {
                        "type": "integer",
                        "default": 2,
                        "description": "Maximum link recursion depth (1 to 4)."
                    },
                    "max_pages": {
                        "type": "integer",
                        "default": 15,
                        "description": "Maximum number of pages to crawl (cap 50)."
                    },
                    "domain_restricted": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to restrict crawling strictly to the starting domain."
                    }
                },
                "required": ["start_url"]
            },
            "side_effects": "creates_directory_and_files",
            "timeout_ms": 60000,
            "memory_limit_mb": 250
        }
        super().__init__("crawl_website", "creates_file", declaration)
        self.crawler = crawler

    async def execute(self, executor, **kwargs) -> dict:
        start_url = kwargs.get("start_url") or kwargs.get("url") or kwargs.get("source") or ""
        start_url = start_url.strip()
        if not start_url:
            return {"status": "failure", "message": "Missing required parameter 'start_url'."}

        max_depth = int(kwargs.get("max_depth", 2))
        max_pages = int(kwargs.get("max_pages", 15))
        domain_restricted = bool(kwargs.get("domain_restricted", True))

        try:
            result = await self.crawler.crawl(
                start_url=start_url,
                max_depth=max_depth,
                max_pages=max_pages,
                domain_restricted=domain_restricted
            )
            return {
                "status": result.status,
                "response": result.message,
                "domain": result.domain,
                "pages_crawled": result.pages_crawled,
                "total_words": result.total_words,
                "output_dir": result.output_dir,
                "index_file": result.index_file,
                "duration_s": result.duration_s
            }
        except Exception as e:
            logger.error(f"Error in crawl_website: {e}")
            return {"status": "failure", "message": f"Website crawl failed: {e}"}


class FetchDynamicPageTool(Tool):
    """
    Renders dynamic JavaScript Single Page Applications (React, Vue, Angular)
    using headless Playwright Chromium and returns clean inner text.
    """

    def __init__(self, fetcher: DynamicBrowserFetcher):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to render with headless browser."
                    },
                    "wait_selector": {
                        "type": "string",
                        "description": "Optional CSS selector to wait for before extracting content."
                    },
                    "take_screenshot": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to capture a PNG screenshot."
                    }
                },
                "required": ["url"]
            },
            "side_effects": "none",
            "timeout_ms": 25000,
            "memory_limit_mb": 200
        }
        super().__init__("fetch_dynamic_page", "read_only", declaration)
        self.fetcher = fetcher

    async def execute(self, executor, **kwargs) -> dict:
        url = kwargs.get("url") or kwargs.get("source") or ""
        url = url.strip()
        if not url:
            return {"status": "failure", "message": "Missing required parameter 'url'."}

        wait_selector = kwargs.get("wait_selector")
        take_screenshot = bool(kwargs.get("take_screenshot", False))

        try:
            result = await self.fetcher.fetch_dynamic_page(
                url=url,
                wait_selector=wait_selector,
                take_screenshot=take_screenshot
            )
            if result.status != "success":
                return {"status": "failure", "message": result.message}

            resp_lines = [
                f"Rendered Page: '{result.title}'",
                f"Source: {result.url} (Execution time: {result.duration_s}s)",
                f"Links discovered: {len(result.links)}",
                "",
                result.text[:1000] + ("..." if len(result.text) > 1000 else "")
            ]
            if result.screenshot_path:
                resp_lines.append(f"\nScreenshot saved: {result.screenshot_path}")

            return {
                "status": "success",
                "response": "\n".join(resp_lines),
                "title": result.title,
                "url": result.url,
                "text": result.text,
                "links": result.links,
                "screenshot_path": result.screenshot_path,
                "duration_s": result.duration_s
            }
        except Exception as e:
            logger.error(f"Error in fetch_dynamic_page: {e}")
            return {"status": "failure", "message": f"Dynamic page fetch failed: {e}"}
