"""Check public site links and send problem URLs to a Sheets webhook."""

import argparse
import csv
import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests


DEFAULT_TARGET = "https://www.emservices.com.sg/"
USER_AGENT = "DeadLinkCheckerBot/1.0 (scheduled site link audit)"
TIMEOUT = (5, 15)
MAX_HTML_BYTES = 5_000_000
RESOURCE_EXTENSIONS = (
    ".avif", ".css", ".csv", ".doc", ".docx", ".eot", ".gif", ".ico",
    ".jpeg", ".jpg", ".js", ".json", ".mp3", ".mp4", ".pdf", ".png",
    ".svg", ".txt", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".xml",
    ".zip",
)


class ScanIncomplete(RuntimeError):
    """A scan stopped before every discovered URL was checked."""


@dataclass(frozen=True)
class Issue:
    status: str
    url: str
    source_text: str


class LinkParser(HTMLParser):
    """Collect navigation links and common linked assets from an HTML page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.base_href = None
        self._anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href") and self.base_href is None:
            self.base_href = attrs["href"]
        if tag == "a" and attrs.get("href"):
            self._anchor = [attrs["href"], []]
        elif tag in ("img", "script", "iframe", "source") and attrs.get("src"):
            self.links.append((attrs["src"], tag))
        elif tag == "link" and attrs.get("href"):
            self.links.append((attrs["href"], "link"))
        if tag in ("img", "source") and attrs.get("srcset"):
            for candidate in attrs["srcset"].split(","):
                url = candidate.strip().split()
                if url:
                    self.links.append((url[0], f"{tag} srcset"))

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            url, parts = self._anchor
            self.links.append((url, " ".join("".join(parts).split()) or "link"))
            self._anchor = None


def normalize_url(raw, base):
    """Return an HTTP URL without its fragment, or None for non-web links."""
    if not raw or raw.strip().startswith("#"):
        return None
    try:
        joined = urljoin(base, raw.strip())
        joined, _ = urldefrag(joined)
        parts = urlsplit(joined)
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    # URL fragments do not change the HTTP resource. Keep query strings.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def check_url(url):
    """Check one linked URL without downloading its full response body."""
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True) as response:
            if response.status_code >= 400:
                return f"HTTP {response.status_code}"
            return None
    except requests.RequestException as exc:
        return f"ERROR {type(exc).__name__}"


class SiteScanner:
    def __init__(self, target, max_pages=750, max_urls=5000, deadline_seconds=1500):
        self.target = normalize_url(target, target)
        if not self.target:
            raise ValueError(f"Invalid TARGET_URL: {target!r}")
        self.host = urlsplit(self.target).hostname
        self.port = urlsplit(self.target).port
        self.max_pages = max_pages
        self.max_urls = max_urls
        self.deadline = time.monotonic() + deadline_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.pages = deque()
        self.check_only = set()
        self.discovered = set()
        self.checked = set()
        self.sources = {}
        self.problems = {}
        self.page_count = 0
        self.sitemap_count = 0
        self.robots = None

    @property
    def issues(self):
        return [Issue(status, url, self.sources.get(url, "Sitemap"))
                for url, status in self.problems.items()]

    def _time_left(self):
        if time.monotonic() >= self.deadline:
            raise ScanIncomplete("Scan deadline reached before all links were checked")

    def _internal(self, url):
        parts = urlsplit(url)
        host = parts.hostname
        return parts.port == self.port and (host == self.host or host == self.host.removeprefix("www."))

    def _allowed(self, url):
        return not self._internal(url) or self.robots is None or self.robots.can_fetch(USER_AGENT, url)

    def _add(self, url, label, source, crawl=False):
        if not url:
            return
        if url in self.discovered:
            if label != "Sitemap" and self.sources[url].startswith("Sitemap |"):
                self.sources[url] = f"{label[:120]} | source: {source}"[:500]
            return
        if not self._allowed(url):
            return
        self.discovered.add(url)
        if len(self.discovered) > self.max_urls:
            raise ScanIncomplete(f"MAX_URLS={self.max_urls} reached; report is partial")
        self.sources[url] = f"{label[:120]} | source: {source}"[:500]
        if crawl and self._internal(url) and not urlsplit(url).path.lower().endswith(RESOURCE_EXTENSIONS):
            self.pages.append(url)
        else:
            self.check_only.add(url)

    def _request(self, url, read_html=False):
        self._time_left()
        try:
            with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True) as response:
                status = response.status_code
                final_url = response.url
                content_type = response.headers.get("Content-Type", "").lower()
                html = None
                if read_html and status < 400 and "text/html" in content_type and self._internal(final_url):
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(chunk_size=65536):
                        self._time_left()
                        size += len(chunk)
                        if size > MAX_HTML_BYTES:
                            raise ScanIncomplete(f"HTML page exceeds {MAX_HTML_BYTES} bytes: {url}")
                        chunks.append(chunk)
                    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return status, html, final_url
        except requests.RequestException as exc:
            return f"ERROR {type(exc).__name__}", None, url

    def _record(self, url, status):
        if isinstance(status, str) or status >= 400:
            label = status if isinstance(status, str) else f"HTTP {status}"
            self.problems[url] = label

    def _load_robots_and_sitemaps(self):
        robots_url = urljoin(self.target, "/robots.txt")
        try:
            response = self.session.get(robots_url, timeout=TIMEOUT)
            if response.status_code != 200:
                print(f"robots.txt returned HTTP {response.status_code}; following page links only", flush=True)
                return
            robots_text = response.text
        except requests.RequestException as exc:
            print(f"robots.txt unavailable ({type(exc).__name__}); following page links only", flush=True)
            return

        self.robots = RobotFileParser()
        self.robots.parse(robots_text.splitlines())
        sitemap_urls = [line.partition(":")[2].strip() for line in robots_text.splitlines()
                        if line.lower().startswith("sitemap:")]
        pending = deque(normalize_url(url, self.target) for url in sitemap_urls)
        visited = set()
        while pending:
            self._time_left()
            sitemap_url = pending.popleft()
            if not sitemap_url or sitemap_url in visited or not self._internal(sitemap_url):
                continue
            visited.add(sitemap_url)
            if len(visited) > 30:
                raise ScanIncomplete("More than 30 sitemaps found; report is partial")
            try:
                response = self.session.get(sitemap_url, timeout=TIMEOUT)
                response.raise_for_status()
                root = ElementTree.fromstring(response.content)
            except (requests.RequestException, ElementTree.ParseError) as exc:
                raise ScanIncomplete(f"Could not read advertised sitemap {sitemap_url}: {exc}") from exc
            self.sitemap_count += 1
            kind = root.tag.rsplit("}", 1)[-1]
            if kind == "sitemapindex":
                for element in root.findall("./{*}sitemap/{*}loc"):
                    pending.append(normalize_url(element.text, sitemap_url))
            elif kind == "urlset":
                for element in root.findall("./{*}url/{*}loc"):
                    url = normalize_url(element.text, sitemap_url)
                    if url and self._internal(url):
                        self._add(url, "Sitemap", sitemap_url, crawl=True)

    def _crawl_pages(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            while self.pages:
                self._time_left()
                batch = []
                while self.pages and len(batch) < 3:
                    url = self.pages.popleft()
                    if url not in self.checked:
                        batch.append(url)
                if self.page_count + len(batch) > self.max_pages:
                    raise ScanIncomplete(f"MAX_PAGES={self.max_pages} reached; report is partial")
                futures = {pool.submit(self._request, url, True): url for url in batch}
                for future in as_completed(futures):
                    self._time_left()
                    url = futures[future]
                    status, html, final_url = future.result()
                    self.checked.add(url)
                    self.page_count += 1
                    self._record(url, status)
                    if url == self.target and (isinstance(status, str) or status >= 400):
                        raise ScanIncomplete(f"Target homepage unavailable: {status}")
                    if url == self.target and not html:
                        raise ScanIncomplete("Target homepage did not return in-scope HTML")
                    if html:
                        parser = LinkParser()
                        parser.feed(html)
                        base = normalize_url(parser.base_href, final_url) if parser.base_href else final_url
                        for raw, label in parser.links:
                            linked_url = normalize_url(raw, base or final_url)
                            self._add(linked_url, label, final_url, crawl=True)
                    if self.page_count % 25 == 0:
                        print(f"Pages: {self.page_count}; URLs found: {len(self.discovered)}; issues: {len(self.issues)}", flush=True)
                time.sleep(0.2)

    def _check_remaining_links(self):
        remaining = sorted(self.check_only - self.checked)
        if not remaining:
            return
        with ThreadPoolExecutor(max_workers=6) as pool:
            for offset in range(0, len(remaining), 6):
                self._time_left()
                batch = remaining[offset:offset + 6]
                futures = {pool.submit(check_url, url): url for url in batch}
                for future in as_completed(futures):
                    self._time_left()
                    url = futures[future]
                    self.checked.add(url)
                    status = future.result()
                    if status:
                        self._record(url, status)
                if len(self.checked) % 100 < 6:
                    print(f"Checked: {len(self.checked)} URLs; issues: {len(self.issues)}", flush=True)
        self._time_left()

    def scan(self):
        print(f"Checking {self.target}", flush=True)
        self._add(self.target, "Target", self.target, crawl=True)
        self._load_robots_and_sitemaps()
        self._crawl_pages()
        self._check_remaining_links()
        print(f"Complete: {self.page_count} pages, {len(self.checked)} URLs, "
              f"{len(self.issues)} issues, {self.sitemap_count} sitemaps", flush=True)
        return self.issues


def write_report(issues, path="scan_report.csv"):
    with open(path, "w", encoding="utf-8", newline="") as report:
        writer = csv.writer(report)
        writer.writerow(["timestamp", "status", "url", "source_text"])
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for issue in sorted(issues, key=lambda item: (item.url, item.status)):
            writer.writerow([timestamp, issue.status, issue.url, issue.source_text])


def write_status(scanner, complete, error, path="scan_status.json"):
    with open(path, "w", encoding="utf-8") as status_file:
        json.dump({
            "scan_complete": complete,
            "error": error,
            "target": scanner.target,
            "pages_checked": scanner.page_count,
            "urls_discovered": len(scanner.discovered),
            "urls_checked": len(scanner.checked),
            "issues": len(scanner.issues),
            "sitemaps_read": scanner.sitemap_count,
        }, status_file, indent=2)
        status_file.write("\n")


def push_to_sheets(issues, webhook_url):
    if not issues:
        print("No problematic links to send to Sheets", flush=True)
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = [[timestamp, issue.status, issue.url, issue.source_text] for issue in issues]
    try:
        response = requests.post(webhook_url, json={"rows": rows}, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(f"Sheets webhook request failed: {type(exc).__name__}") from None
    if not response.ok:
        raise RuntimeError(f"Sheets webhook returned HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError:
        result = None
    if isinstance(result, dict) and (
        result.get("success") is False or result.get("status") in ("error", "failed")
        or result.get("error")
    ):
        raise RuntimeError("Sheets webhook reported an error")
    print(f"Sent {len(rows)} rows to Sheets (HTTP {response.status_code})", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="scan and write CSV without posting to Sheets")
    args = parser.parse_args()
    webhook_url = os.environ.get("SHEET_WEBHOOK_URL")
    if not args.dry_run and not webhook_url:
        raise RuntimeError("SHEET_WEBHOOK_URL is required")
    scanner = SiteScanner(os.environ.get("TARGET_URL", DEFAULT_TARGET))
    complete = False
    error = None
    try:
        issues = scanner.scan()
        complete = True
        if not args.dry_run:
            push_to_sheets(issues, webhook_url)
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        write_report(scanner.issues)
        write_status(scanner, complete, error)
        scanner.session.close()


if __name__ == "__main__":
    main()
