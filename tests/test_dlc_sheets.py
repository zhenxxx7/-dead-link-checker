import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from dlc_sheets import Issue, ScanIncomplete, SiteScanner, normalize_url, push_to_sheets


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.calls.append(path)
        base = f"http://127.0.0.1:{self.server.server_port}"
        if path == "/robots.txt":
            body = f"User-agent: *\nDisallow: /private\nSitemap: {base}/sitemap.xml\n"
            self.send_body(200, body, "text/plain")
        elif path == "/sitemap.xml":
            body = (
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"<url><loc>{base}/</loc></url><url><loc>{base}/hidden</loc></url>"
                f"<url><loc>{base}/sitemap-broken</loc></url>"
                "</urlset>"
            )
            self.send_body(200, body, "application/xml")
        elif path == "/":
            self.send_body(
                200,
                '<a href="/good">Good page</a>'
                '<a href="/missing#fragment">Missing page</a>'
                '<a href="/private">Do not crawl</a>'
                '<img src="/asset.png">'
                '<link rel="alternate" type="application/json" href="/meta">',
                "text/html",
            )
        elif path == "/good":
            self.send_body(
                200,
                '<a href="/missing">Duplicate missing</a>'
                '<a href="/redirect">Moved</a>'
                '<a href="/moved">Redirected page</a>'
                '<a href="/sitemap-broken">Listed broken</a>',
                "text/html",
            )
        elif path == "/hidden":
            self.send_body(200, '<a href="/missing2">Other missing</a>', "text/html")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/missing3")
            self.end_headers()
        elif path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/dir/page")
            self.end_headers()
        elif path == "/dir/page":
            self.send_body(200, '<a href="relative-missing">Relative broken</a>', "text/html")
        elif path in ("/missing", "/missing2", "/missing3", "/sitemap-broken", "/dir/relative-missing"):
            self.send_body(404, "not found", "text/plain")
        elif path == "/blank":
            self.send_body(200, "not HTML", "text/plain")
        elif path == "/asset.png":
            self.send_body(200, "image", "image/png")
        elif path == "/meta":
            self.send_body(200, '{"page": true}', "application/json")
        else:
            self.send_body(500, "unexpected path", "text/plain")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.posts.append(json.loads(body))
        if self.path == "/webhook-error":
            self.send_body(503, "unavailable", "text/plain")
        elif self.path == "/webhook-json-error":
            self.send_body(200, '{"success": false}', "application/json")
        else:
            self.send_body(200, "ok", "text/plain")

    def send_body(self, status, body, content_type):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


class ScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.server.calls = []
        cls.server.posts = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.server.calls.clear()
        self.server.posts.clear()

    def test_url_normalization(self):
        self.assertEqual(normalize_url("/missing#part", self.base), self.base + "/missing")
        self.assertIsNone(normalize_url("mailto:admin@example.com", self.base))
        self.assertIsNone(normalize_url("#section", self.base))
        self.assertIsNone(normalize_url("http://[", self.base))
        self.assertIsNone(normalize_url("http://example.com:bad/", self.base))

    def test_crawl_sitemap_links_resources_and_robots(self):
        scanner = SiteScanner(self.base + "/", max_pages=20, max_urls=30, deadline_seconds=30)
        try:
            issues = scanner.scan()
        finally:
            scanner.session.close()
        self.assertEqual(
            {issue.url for issue in issues},
            {
                self.base + "/missing",
                self.base + "/missing2",
                self.base + "/redirect",
                self.base + "/sitemap-broken",
                self.base + "/dir/relative-missing",
            },
        )
        self.assertTrue(all(issue.status == "HTTP 404" for issue in issues))
        self.assertEqual(self.server.calls.count("/missing"), 1)
        self.assertIn("/hidden", self.server.calls)
        self.assertIn("/asset.png", self.server.calls)
        self.assertIn("/meta", self.server.calls)
        self.assertEqual(scanner.page_count, 9)
        self.assertNotIn("/private", self.server.calls)
        listed_issue = next(issue for issue in issues if issue.url.endswith("/sitemap-broken"))
        self.assertTrue(listed_issue.source_text.startswith("Listed broken | source:"))

    def test_non_html_target_fails(self):
        scanner = SiteScanner(self.base + "/blank", max_pages=20, max_urls=30, deadline_seconds=30)
        try:
            with self.assertRaisesRegex(ScanIncomplete, "in-scope HTML"):
                scanner.scan()
        finally:
            scanner.session.close()

    def test_page_limit_fails_instead_of_reporting_complete(self):
        scanner = SiteScanner(self.base + "/", max_pages=1, max_urls=30, deadline_seconds=30)
        try:
            with self.assertRaisesRegex(ScanIncomplete, "MAX_PAGES"):
                scanner.scan()
        finally:
            scanner.session.close()

    def test_webhook_rows_keep_existing_shape(self):
        issue = Issue("HTTP 404", self.base + "/missing", "Missing page | source: /")
        push_to_sheets([issue], self.base + "/webhook")
        rows = self.server.posts[0]["rows"]
        self.assertEqual(len(rows[0]), 4)
        self.assertEqual(rows[0][1:], [issue.status, issue.url, issue.source_text])

    def test_webhook_rejection_fails_without_exposing_its_url(self):
        issue = Issue("HTTP 404", self.base + "/missing", "Missing page")
        with self.assertRaisesRegex(RuntimeError, "returned HTTP 503") as http_error:
            push_to_sheets([issue], self.base + "/webhook-error")
        self.assertNotIn(self.base, str(http_error.exception))
        with self.assertRaisesRegex(RuntimeError, "reported an error"):
            push_to_sheets([issue], self.base + "/webhook-json-error")


if __name__ == "__main__":
    unittest.main()
