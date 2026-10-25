#!/usr/bin/env python3
"""Rebuild the root of the account's GitHub Pages host.

Crawlers read robots.txt only at a host's root. Every project site here lives under
https://<owner>.github.io/<repo>/, so until this repo existed the host had no robots.txt
and no sitemap at all. Each run:

1. lists the account's public, non-fork repos with GitHub Pages turned on (GitHub REST API);
2. probes each site at https://<owner>.github.io/<repo>/, following any redirect to a
   custom domain, and keeps the ones that answer 200;
3. finds each live site's sitemaps (the Sitemap: lines of its robots.txt, else
   /sitemap.xml) and keeps the ones that parse as a sitemap or sitemap index;
4. writes site/robots.txt, site/sitemap.xml (a sitemap index), site/sitemap-root.xml,
   site/index.html and site/sites.json.

A site that stops answering stays listed for GRACE_DAYS, so one bad probe can't drop it.
If discovery itself fails (the API is down, or nothing at all is live), the script exits
non-zero and writes nothing, so the last good files stay deployed. Output only changes
when the list of sites or their sitemaps change, so an unchanged run commits nothing.

  python3 scripts/build_root.py             discover sites and rewrite site/
  python3 scripts/build_root.py --check     validate site/ (no network)
  python3 scripts/build_root.py --live      check the deployed files on the live host

Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
OWNER = os.environ.get("ROOT_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "theguysaccount"
HOST = f"{OWNER.lower()}.github.io"
BASE = f"https://{HOST}/"
REPO_URL = f"https://github.com/{OWNER}/{HOST}"
UA = f"Mozilla/5.0 (compatible; {HOST}-root-builder/1.0; +{REPO_URL})"
GRACE_DAYS = 3            # how long a site that stops answering stays listed
TIMEOUT = 25
MAX_INDEX_ENTRIES = 50000  # sitemaps.org limit for one sitemap index
SM_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
REDIRECTS = (301, 302, 303, 307, 308)


# ---------------------------------------------------------------- small helpers

def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_between(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def log(msg: str) -> None:
    print(msg, flush=True)


def annotate(level: str, msg: str) -> None:
    """A GitHub Actions annotation (shows on the run page); plain text elsewhere."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{msg}", flush=True)
    else:
        print(f"{level.upper()}: {msg}", flush=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url: str, *, follow: bool = True, api: bool = False, accept: str = "*/*",
            limit: int = 30_000_000, tries: int = 2) -> tuple[int, str, dict, bytes]:
    """Fetch a URL. Returns (status, final_url, headers, body); status 0 means no answer."""
    headers = {"User-Agent": UA, "Accept": accept}
    if api:
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(_NoRedirect)
    result = (0, url, {}, b"")
    for attempt in range(tries):
        if attempt:
            time.sleep(3 * attempt)
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as r:
                return r.status, r.geturl(), {k.lower(): v for k, v in r.headers.items()}, r.read(limit)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read(limit)
            except Exception:
                pass
            result = (e.code, url, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
            if e.code < 500 and e.code != 429:
                return result
        except (urllib.error.URLError, OSError, ValueError) as e:
            result = (0, url, {"error": str(e)}, b"")
    return result


def maybe_gunzip(body: bytes) -> bytes:
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------- discovery

def list_pages_repos() -> list[dict]:
    """Public, non-fork repos of OWNER with GitHub Pages turned on (not this repo)."""
    repos, page = [], 1
    while True:
        status, _, _, body = request(
            f"https://api.github.com/users/{OWNER}/repos?type=owner&per_page=100&page={page}", api=True, tries=3)
        if status != 200:
            raise RuntimeError(f"the GitHub API answered {status} when listing {OWNER}'s repos")
        batch = json.loads(body)
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    keep = [r for r in repos if r.get("has_pages") and not r.get("private") and not r.get("fork")
            and r["name"].lower() != HOST]
    return sorted(keep, key=lambda r: r["name"].lower())


def probe(name: str) -> tuple[str, bytes] | None:
    """Follow https://HOST/<name>/ to wherever it is served. Returns (final_url, html) or None."""
    url = f"{BASE}{name}/"
    for _ in range(6):
        status, _, headers, body = request(url, follow=False, accept="text/html,*/*;q=0.8", limit=600_000)
        if status in REDIRECTS and headers.get("location"):
            url = urllib.parse.urljoin(url, headers["location"])
            continue
        if status == 200:
            parts = urllib.parse.urlsplit(url)
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")), body
        return None
    return None


def page_meta(body: bytes) -> tuple[str, str]:
    text = body[:400_000].decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    desc = (re.search(r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']', text, re.I)
            or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']description["\']', text, re.I))
    tidy = lambda s: re.sub(r"\s+", " ", html.unescape(s or "")).strip()  # noqa: E731
    return tidy(title.group(1) if title else ""), tidy(desc.group(1) if desc else "")


def robots_sitemaps(text: str) -> list[str]:
    found = []
    for line in text.splitlines():
        m = re.match(r"\s*sitemap\s*:\s*(\S+)", line, re.I)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def parse_sitemap(body: bytes) -> dict | None:
    """{'type': 'urlset'|'sitemapindex', 'count': n, 'children': [(loc, lastmod)]} or None."""
    try:
        root = ET.fromstring(maybe_gunzip(body))
    except (ET.ParseError, OSError, EOFError):
        return None
    kind = local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        return None
    entries = []
    for el in root:
        loc = lastmod = ""
        for child in el:
            if local(child.tag) == "loc":
                loc = (child.text or "").strip()
            elif local(child.tag) == "lastmod":
                lastmod = (child.text or "").strip()
        if loc:
            entries.append((loc, lastmod))
    return {"type": kind, "count": len(entries), "children": entries if kind == "sitemapindex" else []}


def belongs(url: str, site_url: str) -> bool:
    """A sitemap belongs to a site when it sits on the same host and under the site's path."""
    u, s = urllib.parse.urlsplit(url), urllib.parse.urlsplit(site_url)
    return u.netloc.lower() == s.netloc.lower() and u.path.lower().startswith((s.path or "/").lower())


def find_sitemaps(site_url: str) -> list[dict]:
    status, _, _, body = request(urllib.parse.urljoin(site_url, "robots.txt"), limit=200_000)
    declared = robots_sitemaps(body.decode("utf-8", "replace")) if status == 200 else []
    candidates = [u for u in declared if belongs(u, site_url)] or [urllib.parse.urljoin(site_url, "sitemap.xml")]
    found = []
    for url in candidates:
        status, _, _, body = request(url, accept="application/xml,text/xml,*/*;q=0.8")
        parsed = parse_sitemap(body) if status == 200 else None
        if parsed:
            children = [(loc, mod) for loc, mod in parsed["children"] if belongs(loc, site_url)]
            found.append({"url": url, "type": parsed["type"], "count": parsed["count"],
                          "children": [{"loc": loc, "lastmod": mod} for loc, mod in children]})
        else:
            log(f"  sitemap {url}: not usable (HTTP {status})")
    return found


def discover(previous: dict) -> list[dict]:
    """The live sites, merged with the previous list so a transient failure can't drop a site."""
    now = today()
    prev = {s["repo"]: s for s in previous.get("sites", [])}
    sites = []
    for repo in list_pages_repos():
        name = repo["name"]
        hit = probe(name)
        if not hit:
            old = prev.get(name)
            if old:
                since = old.get("failing_since") or now
                if days_between(since, now) <= GRACE_DAYS:
                    annotate("warning", f"{name}: no answer at {old['url']} (failing since {since}); kept for now")
                    sites.append(dict(old, failing_since=since))
                else:
                    annotate("warning", f"{name}: no answer since {since}; dropped from the list")
            else:
                log(f"  {name}: Pages is on but nothing answers at {BASE}{name}/; skipped")
            continue
        url, body = hit
        title, description = page_meta(body)
        sitemaps = find_sitemaps(url)
        old = prev.get(name, {})
        if not sitemaps and old.get("sitemaps") and old.get("url") == url:
            annotate("warning", f"{name}: its sitemaps did not answer this run; keeping the last good ones")
            sitemaps = old["sitemaps"]
        sites.append({
            "repo": name,
            "url": url,
            "kind": "path" if urllib.parse.urlsplit(url).netloc.lower() == HOST else "domain",
            "title": title or name,
            "description": description or (repo.get("description") or "").strip(),
            "source": repo["html_url"],
            "sitemaps": sitemaps,
            "first_seen": old.get("first_seen") or now,
            "failing_since": None,
        })
        log(f"  {name}: live at {url}; {len(sitemaps)} sitemap(s)")
    return sites


# ---------------------------------------------------------------- rendering

def render_robots(sites: list[dict]) -> str:
    on_host = [sm["url"] for s in sites if s["kind"] == "path" for sm in s["sitemaps"]]
    elsewhere = [sm["url"] for s in sites if s["kind"] == "domain" for sm in s["sitemaps"]]
    lines = [
        f"# {BASE}robots.txt",
        "# Crawlers read robots.txt only at a host's root, so this file speaks for every",
        f"# project site served under {BASE}<repo>/.",
        "# Rebuilt daily from the account's live GitHub Pages sites by",
        f"# {REPO_URL}",
        "",
        "User-agent: *",
        "Allow: /",
        "",
        "# This host: the root sitemap index, then each project's own sitemap.",
        f"Sitemap: {BASE}sitemap.xml",
    ]
    lines += [f"Sitemap: {u}" for u in on_host]
    if elsewhere:
        lines += ["", "# Project sites on their own domains. Each domain's own robots.txt is the one",
                  "# crawlers obey there; these lines only help discovery."]
        lines += [f"Sitemap: {u}" for u in elsewhere]
    return "\n".join(lines) + "\n"


def render_sitemap_index(sites: list[dict], changed_at: str) -> str:
    entries = [(f"{BASE}sitemap-root.xml", changed_at)]
    for s in sites:
        if s["kind"] != "path":
            continue  # a sitemap index may only point at sitemaps on its own host
        for sm in s["sitemaps"]:
            if sm["type"] == "sitemapindex":  # indexes can't nest, so list the children instead
                entries += [(c["loc"], c.get("lastmod", "")) for c in sm["children"]]
            else:
                entries.append((sm["url"], ""))
    seen, rows = set(), []
    for loc, mod in entries[:MAX_INDEX_ENTRIES]:
        if loc in seen:
            continue
        seen.add(loc)
        lastmod = f"<lastmod>{html.escape(mod)}</lastmod>" if mod else ""
        rows.append(f"<sitemap><loc>{html.escape(loc)}</loc>{lastmod}</sitemap>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="{SM_NS}">\n'
            + "\n".join(rows) + "\n</sitemapindex>\n")


def render_root_sitemap(changed_at: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="{SM_NS}">\n'
            f"<url><loc>{BASE}</loc><lastmod>{changed_at}</lastmod></url>\n</urlset>\n")


def render_index(sites: list[dict], changed_at: str) -> str:
    e = lambda v: html.escape(str(v), quote=True)  # noqa: E731
    cards = []
    for s in sites:
        shown = s["url"].split("://", 1)[-1].rstrip("/")
        desc = f'<p>{e(s["description"])}</p>' if s["description"] else ""
        note = ' <span class="note">(not answering; rechecked daily)</span>' if s.get("failing_since") else ""
        cards.append(
            f'<li><h2><a href="{e(s["url"])}">{e(s["title"])}</a>{note}</h2>{desc}'
            f'<p class="meta"><a href="{e(s["url"])}">{e(shown)}</a> · <a href="{e(s["source"])}">source on GitHub</a></p></li>')
    description = (f"Index of the project sites {OWNER} publishes on GitHub Pages, "
                   "rebuilt automatically from the GitHub API.")
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "CollectionPage", "name": f"Projects on {HOST}",
        "url": BASE, "description": description,
        "hasPart": [{"@type": "WebSite", "name": s["title"], "url": s["url"]} for s in sites],
    }).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Projects on {e(HOST)}</title>
<meta name="description" content="{e(description)}">
<link rel="canonical" href="{BASE}">
<meta property="og:type" content="website">
<meta property="og:title" content="Projects on {e(HOST)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{BASE}">
<meta name="color-scheme" content="light dark">
<script type="application/ld+json">{ld}</script>
<style>
:root {{ --bg: #fbfbf9; --ink: #1d1d1b; --muted: #5f5f5a; --line: #e2e1dc; --link: #1f4fd1; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #141413; --ink: #ecebe6; --muted: #a3a29c; --line: #2c2c2a; --link: #8fb0ff; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 48px 16px 64px; }}
h1 {{ font-size: 1.6rem; margin: 0 0 8px; }}
.lede {{ color: var(--muted); margin: 0 0 32px; }}
ul {{ list-style: none; margin: 0; padding: 0; }}
li {{ border-top: 1px solid var(--line); padding: 18px 0; }}
h2 {{ font-size: 1.1rem; margin: 0 0 4px; }}
p {{ margin: 0 0 6px; }}
a {{ color: var(--link); }}
.meta, .note, footer {{ color: var(--muted); font-size: .9rem; }}
.meta a {{ color: inherit; }}
footer {{ border-top: 1px solid var(--line); margin-top: 8px; padding-top: 18px; }}
</style>
</head>
<body>
<main>
<h1>Projects on {e(HOST)}</h1>
<p class="lede">The live project sites this GitHub account publishes on GitHub Pages.</p>
<ul>
{chr(10).join(cards)}
</ul>
<footer>
<p>This page, <a href="robots.txt">robots.txt</a> and the <a href="sitemap.xml">sitemap index</a> are rebuilt every day from the GitHub API, so new project sites appear here on their own. List last changed {e(changed_at)}. Machine-readable: <a href="sites.json">sites.json</a>. <a href="{e(REPO_URL)}">Source</a>.</p>
</footer>
</main>
</body>
</html>
"""


def material(sites: list[dict]) -> str:
    return json.dumps(sites, sort_keys=True)


def write_site(sites: list[dict], previous: dict) -> bool:
    """Write every output file. Returns True when the site list changed."""
    changed = material(sites) != material(previous.get("sites", []))
    changed_at = today() if changed or not previous.get("changed_at") else previous["changed_at"]
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "robots.txt").write_text(render_robots(sites), encoding="utf-8")
    (SITE / "sitemap.xml").write_text(render_sitemap_index(sites, changed_at), encoding="utf-8")
    (SITE / "sitemap-root.xml").write_text(render_root_sitemap(changed_at), encoding="utf-8")
    (SITE / "index.html").write_text(render_index(sites, changed_at), encoding="utf-8")
    (SITE / "sites.json").write_text(json.dumps(
        {"host": HOST, "changed_at": changed_at, "grace_days": GRACE_DAYS, "sites": sites},
        indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    return changed


# ---------------------------------------------------------------- checks

def check_files(site_dir: Path = SITE) -> list[str]:
    """Problems with the generated files; empty means they are safe to deploy."""
    problems = []
    try:
        data = json.loads((site_dir / "sites.json").read_text(encoding="utf-8"))
        sites = data["sites"]
    except (OSError, ValueError, KeyError) as e:
        return [f"sites.json unreadable: {e}"]
    if not sites:
        problems.append("sites.json lists no sites")
    robots = (site_dir / "robots.txt").read_text(encoding="utf-8") if (site_dir / "robots.txt").exists() else ""
    if "User-agent: *" not in robots:
        problems.append("robots.txt has no 'User-agent: *' group")
    if re.search(r"^\s*Disallow:\s*/\s*$", robots, re.M):
        problems.append("robots.txt blocks the whole host")
    listed = robots_sitemaps(robots)
    if f"{BASE}sitemap.xml" not in listed:
        problems.append("robots.txt does not list the root sitemap index")
    for s in sites:
        for sm in s["sitemaps"]:
            if sm["url"] not in listed:
                problems.append(f"robots.txt is missing {sm['url']}")
    for name, kind in (("sitemap.xml", "sitemapindex"), ("sitemap-root.xml", "urlset")):
        try:
            parsed = parse_sitemap((site_dir / name).read_bytes())
        except OSError:
            parsed = None
        if not parsed or parsed["type"] != kind or not parsed["count"]:
            problems.append(f"{name} is not a non-empty {kind}")
        elif kind == "sitemapindex":
            bad = [loc for loc, _ in parsed["children"] if urllib.parse.urlsplit(loc).netloc.lower() != HOST]
            if bad:
                problems.append(f"sitemap.xml points off this host: {bad[:3]}")
            if parsed["count"] > MAX_INDEX_ENTRIES:
                problems.append("sitemap.xml has more than 50,000 entries")
    index = (site_dir / "index.html").read_text(encoding="utf-8") if (site_dir / "index.html").exists() else ""
    for s in sites:
        if html.escape(s["url"], quote=True) not in index:
            problems.append(f"index.html does not link {s['url']}")
    return problems


def check_live(site_dir: Path = SITE, wait: int = 0) -> list[str]:
    """Compare the live robots.txt with the generated one. Retries for `wait` seconds (CDN cache)."""
    want = set(robots_sitemaps((site_dir / "robots.txt").read_text(encoding="utf-8")))
    deadline, problems = time.time() + wait, ["not checked"]
    while True:
        problems = []
        status, _, _, body = request(f"{BASE}robots.txt", tries=3)
        if status != 200:
            problems.append(f"{BASE}robots.txt answered HTTP {status}")
        else:
            missing = sorted(want - set(robots_sitemaps(body.decode("utf-8", "replace"))))
            if missing:
                problems.append(f"live robots.txt is missing {missing}")
        for path, kind in (("sitemap.xml", "sitemapindex"), ("", None)):
            status, _, _, body = request(BASE + path, tries=3)
            if status != 200:
                problems.append(f"{BASE}{path} answered HTTP {status}")
            elif kind and (parse_sitemap(body) or {}).get("type") != kind:
                problems.append(f"{BASE}{path} is not a sitemap index")
        if not problems or time.time() >= deadline:
            return problems
        log(f"live check: {problems}; retrying in 30s")
        time.sleep(30)


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true", help="validate site/ without network access")
    ap.add_argument("--live", action="store_true", help="check the deployed files on the live host")
    ap.add_argument("--wait", type=int, default=0, help="with --live: keep retrying for this many seconds")
    args = ap.parse_args(argv)

    if args.check or args.live:
        problems = check_files() if args.check else check_live(wait=args.wait)
        for p in problems:
            annotate("error", p)
        log("OK" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    previous = {}
    if (SITE / "sites.json").exists():
        previous = json.loads((SITE / "sites.json").read_text(encoding="utf-8"))
    log(f"Discovering GitHub Pages sites for {OWNER}…")
    try:
        sites = discover(previous)
    except RuntimeError as e:
        annotate("error", f"Discovery failed: {e}. The last good files stay deployed.")
        return 1
    if not sites:
        annotate("error", "Discovery found no live sites at all; refusing to publish an empty host root.")
        return 1
    if previous.get("sites") and len(sites) < len(previous["sites"]) / 2:
        annotate("error", f"Discovery found {len(sites)} sites, under half of the {len(previous['sites'])} "
                          "listed last time; refusing to shrink the list in one run.")
        return 1
    changed = write_site(sites, previous)
    problems = check_files()
    for p in problems:
        annotate("error", p)
    if problems:
        return 1
    log(f"{len(sites)} live site(s); list {'changed' if changed else 'unchanged'}.")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write("urls=" + " ".join(s["url"] for s in sites if s["kind"] == "path") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
