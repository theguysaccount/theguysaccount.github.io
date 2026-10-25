#!/usr/bin/env python3
"""Tell IndexNow search engines (Bing, Yandex, Seznam, Naver, ...) which URLs changed.

  python3 scripts/indexnow.py --key-file site/<key>.txt --base https://host/path/ URL [URL ...]
  python3 scripts/indexnow.py --key-file site/<key>.txt --base https://host/path/ --sitemap <sitemap url>
  python3 scripts/indexnow.py --key-file site/<key>.txt --base https://host/path/ --urls-file urls.txt

The key file holds the key and must be published at <base><key>.txt. IndexNow only accepts
URLs at or below the key file's folder, so anything outside <base> is skipped. A failed
submission is reported but never fails the caller (unless --strict): the site is already
live, and the next deploy submits again. A new key answers 403 until the engines have fetched
the key file once, so 403, 429 and 5xx are retried a few times.

Standard library only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ENDPOINT = "https://api.indexnow.org/indexnow"
BATCH = 10000
UA = "Mozilla/5.0 (compatible; indexnow-submit/1.0)"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def sitemap_urls(url: str, depth: int = 0) -> list[str]:
    root = ET.fromstring(fetch(url))
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
    if root.tag.rsplit("}", 1)[-1] == "sitemapindex" and depth < 1:
        out = []
        for child in locs:
            try:
                out += sitemap_urls(child, depth + 1)
            except Exception as e:  # one bad child sitemap shouldn't sink the rest
                print(f"IndexNow: skipped {child}: {e}")
        return out
    return locs


def submit(base: str, key: str, urls: list[str], retries: int = 3, pause: int = 20) -> int:
    """POST the URLs in batches. Returns the last HTTP status (0 when nothing was sent)."""
    parts = urllib.parse.urlsplit(base)
    inside = [u for u in dict.fromkeys(urls) if u.startswith(base)]
    if len(inside) < len(urls):
        print(f"IndexNow: skipped {len(urls) - len(inside)} URL(s) outside {base}")
    status = 0
    for i in range(0, len(inside), BATCH):
        body = json.dumps({"host": parts.netloc, "key": key, "keyLocation": f"{base}{key}.txt",
                           "urlList": inside[i:i + BATCH]}).encode()
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(pause * attempt)
            req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    status, detail = r.status, ""
            except urllib.error.HTTPError as e:
                status, detail = e.code, e.read()[:300].decode("utf-8", "replace")
            except (urllib.error.URLError, OSError) as e:
                status, detail = 0, str(e)
            print(f"IndexNow: HTTP {status} for {len(inside[i:i + BATCH])} URL(s) {detail}".rstrip())
            if status in (200, 202) or not (status in (0, 403, 429) or status >= 500):
                break
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Submit URLs to IndexNow.")
    ap.add_argument("--key-file", required=True, help="the published key file (<key>.txt)")
    ap.add_argument("--base", required=True, help="the site URL the key file sits in, with a trailing slash")
    ap.add_argument("--sitemap", help="submit every URL in this sitemap or sitemap index")
    ap.add_argument("--urls-file", help="submit the URLs in this file, one per line")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--strict", action="store_true", help="exit 1 unless IndexNow accepted the URLs")
    ap.add_argument("urls", nargs="*")
    args = ap.parse_args(argv)
    base = args.base if args.base.endswith("/") else args.base + "/"
    key = open(args.key_file, encoding="utf-8").read().strip()
    urls = list(args.urls)
    try:
        if args.urls_file:
            urls += [line.strip() for line in open(args.urls_file, encoding="utf-8") if line.strip()]
        if args.sitemap:
            urls += sitemap_urls(args.sitemap)
    except Exception as e:
        print(f"IndexNow: could not collect URLs: {e}")
        return 1 if args.strict else 0
    if not urls:
        print("IndexNow: nothing to submit")
        return 0
    status = submit(base, key, urls, retries=args.retries)
    return 0 if status in (200, 202) or not args.strict else 1


if __name__ == "__main__":
    sys.exit(main())
