"""Offline tests for scripts/build_root.py: parsing, rendering, the grace period and the checks."""
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_root as br  # noqa: E402

B = br.BASE  # https://<owner>.github.io/

URLSET = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.org/</loc></url><url><loc>https://example.org/a</loc></url></urlset>"""
INDEX = f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{B}proj/sitemap-1.xml.gz</loc><lastmod>2026-10-25</lastmod></sitemap>
<sitemap><loc>{B}proj/sitemap-2.xml.gz</loc></sitemap></sitemapindex>""".encode()


def site(repo, url, kind, sitemaps=(), **kw):
    s = {"repo": repo, "url": url, "kind": kind, "title": repo.title(), "description": f"About {repo}",
         "source": f"https://github.com/o/{repo}", "sitemaps": list(sitemaps), "first_seen": "2026-10-01",
         "failing_since": None}
    s.update(kw)
    return s


SITES = [
    site("proj", f"{B}proj/", "path", [{"url": f"{B}proj/sitemap.xml", "type": "sitemapindex", "count": 2,
                                        "children": [{"loc": f"{B}proj/sitemap-1.xml.gz", "lastmod": "2026-10-25"},
                                                     {"loc": f"{B}proj/sitemap-2.xml.gz", "lastmod": ""}]}]),
    site("small", f"{B}small/", "path", [{"url": f"{B}small/sitemap.xml", "type": "urlset", "count": 3, "children": []}]),
    site("civic", "https://civic.example/", "domain",
         [{"url": "https://civic.example/sitemap.xml", "type": "urlset", "count": 9, "children": []}]),
    site("nomap", f"{B}nomap/", "path"),
]


class Parsing(unittest.TestCase):
    def test_robots_sitemaps_case_and_dedupe(self):
        text = "User-agent: *\nsitemap: https://a/x.xml\nSITEMAP:https://a/y.xml\nSitemap: https://a/x.xml\n# Sitemap: no"
        self.assertEqual(br.robots_sitemaps(text), ["https://a/x.xml", "https://a/y.xml"])

    def test_parse_urlset_index_and_gzip(self):
        self.assertEqual(br.parse_sitemap(URLSET)["count"], 2)
        idx = br.parse_sitemap(gzip.compress(INDEX))
        self.assertEqual(idx["type"], "sitemapindex")
        self.assertEqual(idx["children"][0], (f"{B}proj/sitemap-1.xml.gz", "2026-10-25"))

    def test_parse_rejects_html_and_garbage(self):
        self.assertIsNone(br.parse_sitemap(b"<html><body>404</body></html>"))
        self.assertIsNone(br.parse_sitemap(b"not xml"))

    def test_belongs(self):
        self.assertTrue(br.belongs(f"{B}proj/sitemap.xml", f"{B}proj/"))
        self.assertFalse(br.belongs(f"{B}other/sitemap.xml", f"{B}proj/"))
        self.assertTrue(br.belongs("https://civic.example/s.xml", "https://civic.example/"))
        self.assertFalse(br.belongs("https://cdn.example/s.xml", "https://civic.example/"))

    def test_page_meta(self):
        body = b'<html><head><title> My &amp; Site </title><meta name="description" content="Does a thing"></head>'
        self.assertEqual(br.page_meta(body), ("My & Site", "Does a thing"))
        body = b'<meta content="Reversed order" name="description"><title>T</title>'
        self.assertEqual(br.page_meta(body), ("T", "Reversed order"))


class Rendering(unittest.TestCase):
    def test_robots(self):
        text = br.render_robots(SITES)
        self.assertIn("User-agent: *\nAllow: /", text)
        self.assertNotRegex(text, r"(?m)^Disallow:")
        listed = br.robots_sitemaps(text)
        self.assertEqual(listed[0], f"{B}sitemap.xml")
        self.assertEqual(listed[1:], [f"{B}proj/sitemap.xml", f"{B}small/sitemap.xml", "https://civic.example/sitemap.xml"])

    def test_sitemap_index_expands_children_and_stays_on_host(self):
        parsed = br.parse_sitemap(br.render_sitemap_index(SITES, "2026-10-25").encode())
        locs = [loc for loc, _ in parsed["children"]]
        self.assertEqual(locs, [f"{B}sitemap-root.xml", f"{B}proj/sitemap-1.xml.gz", f"{B}proj/sitemap-2.xml.gz",
                                f"{B}small/sitemap.xml"])
        self.assertNotIn(f"{B}proj/sitemap.xml", locs)  # indexes can't nest
        self.assertEqual(parsed["children"][1][1], "2026-10-25")

    def test_index_escapes(self):
        evil = [site("x", f"{B}x/", "path", title="<script>alert(1)</script>")]
        page = br.render_index(evil, "2026-10-25")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)


class WriteAndCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = mock.patch.object(br, "SITE", self.dir)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_roundtrip_passes_check(self):
        self.assertTrue(br.write_site(SITES, {}))
        self.assertEqual(br.check_files(self.dir), [])

    def test_unchanged_run_is_byte_identical(self):
        br.write_site(SITES, {})
        before = {p.name: p.read_bytes() for p in self.dir.iterdir()}
        previous = json.loads((self.dir / "sites.json").read_text())
        previous["changed_at"] = "2026-01-01"
        (self.dir / "sites.json").write_text(json.dumps(previous))
        self.assertFalse(br.write_site(SITES, previous))
        self.assertIn("2026-01-01", (self.dir / "index.html").read_text())
        self.assertEqual(sorted(before), sorted(p.name for p in self.dir.iterdir()))

    def test_check_catches_problems(self):
        br.write_site(SITES, {})
        (self.dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
        problems = " ".join(br.check_files(self.dir))
        self.assertIn("blocks the whole host", problems)
        self.assertIn("root sitemap index", problems)


class Grace(unittest.TestCase):
    def run_discover(self, previous, live, today="2026-10-25"):
        repos = [{"name": n, "html_url": f"https://github.com/o/{n}", "description": ""} for n in ("proj", "small")]
        with mock.patch.object(br, "list_pages_repos", return_value=repos), \
                mock.patch.object(br, "probe", side_effect=lambda n: (f"{B}{n}/", b"<title>T</title>") if n in live else None), \
                mock.patch.object(br, "find_sitemaps", return_value=[]), \
                mock.patch.object(br, "today", return_value=today):
            return {s["repo"]: s for s in br.discover(previous)}

    def test_failing_site_kept_within_grace_then_dropped(self):
        previous = {"sites": [SITES[0], SITES[1]]}
        got = self.run_discover(previous, live={"proj"})
        self.assertEqual(got["small"]["failing_since"], "2026-10-25")
        previous = {"sites": [got["proj"], got["small"]]}
        self.assertIn("small", self.run_discover(previous, live={"proj"}, today="2026-10-28"))
        self.assertNotIn("small", self.run_discover(previous, live={"proj"}, today="2026-10-29"))

    def test_sitemaps_kept_when_they_blink(self):
        got = self.run_discover({"sites": [SITES[0]]}, live={"proj", "small"})
        self.assertEqual(got["proj"]["sitemaps"], SITES[0]["sitemaps"])
        self.assertEqual(got["proj"]["first_seen"], "2026-10-01")
        self.assertIsNone(got["proj"]["failing_since"])


if __name__ == "__main__":
    unittest.main()
