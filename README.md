# theguysaccount.github.io

The root of this account's GitHub Pages host. Every project site lives under
`https://theguysaccount.github.io/<repo>/` (or on its own domain), and crawlers only read
`robots.txt` at a host's root, so this repo gives the host one:

| URL | What it is |
|---|---|
| https://theguysaccount.github.io/robots.txt | Allows all crawling and lists the sitemap of every live project site |
| https://theguysaccount.github.io/sitemap.xml | Sitemap index for this host: the root page plus each project's sitemaps |
| https://theguysaccount.github.io/ | A plain index of the live project sites |
| https://theguysaccount.github.io/sites.json | The same list, machine-readable |

Nothing here is edited by hand. A daily GitHub Actions job asks the GitHub API which public repos
have Pages turned on, probes each site (following redirects to custom domains), reads each one's
sitemaps from its `robots.txt` (falling back to `/sitemap.xml`), and regenerates the files in `site/`.
A new project site shows up within a day, with no change to this repo.

## Run it

```bash
python3 scripts/build_root.py            # discover sites and rewrite site/ (set GITHUB_TOKEN for a higher API limit)
python3 scripts/build_root.py --check    # validate site/ offline
python3 scripts/build_root.py --live     # check the deployed files
python3 -m unittest discover -s tests    # tests
```

Python 3.9+, standard library only.

## Rules the generator follows

- Only public, non-fork repos with Pages on are considered, and only sites that answer HTTP 200.
- A site that stops answering stays listed for 3 days before it is dropped.
- A sitemap only counts if it parses as a `urlset` or `sitemapindex` and sits under the site's own URL.
- The root sitemap index lists only sitemaps on this host (the protocol requires it). Project sitemap
  indexes are expanded into their children, because indexes can't nest.
- Custom-domain sites are listed in `robots.txt` too, under a comment: their own domain's `robots.txt`
  is the one crawlers obey there, and those lines only help discovery.
- Discovery that fails, finds nothing, or would drop more than half the list in one run exits non-zero
  and writes nothing, so the last good deploy stays live.

Operations, failure modes and what to watch: [OPERATIONS.md](OPERATIONS.md).
