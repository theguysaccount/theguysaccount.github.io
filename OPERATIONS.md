# Operations

This repo runs itself. One workflow, `.github/workflows/root-site.yml` ("Rebuild host root"),
does everything.

## Automated jobs

| When | Job | What it does | How it fails safely |
|---|---|---|---|
| Daily 06:17 UTC, every push to `main`, manual dispatch | `build` | Runs the unit tests, discovers the account's live Pages sites, rewrites `site/`, validates it, commits it when the list changed | Discovery errors, an empty result or a list that would halve in one run exit non-zero before anything is written or deployed. The previous deploy stays live. |
| Same run, scheduled only | keepalive step | Commits `.github/keepalive` when the repo has had no commit for 30 days | GitHub disables scheduled workflows in a public repo after 60 quiet days; this keeps the daily schedule alive indefinitely. |
| After `build` | `deploy` | Deploys `site/` to Pages, then checks the live `robots.txt` and `sitemap.xml` for up to 10 minutes | A failed check marks the run red. The files that were live before stay live until a good run replaces them. |
| After `deploy`, when the list changed (or on push/dispatch) | IndexNow ping | Submits the root page and every project homepage on this host to IndexNow (Bing, Yandex, Seznam, Naver) | Never fails the run. The key file is `site/449169a34c1b6da6035789a49931ce4b.txt`. |

## What to watch

- **Actions tab:** a red "Rebuild host root" run. The run page shows `::error::` annotations naming the problem.
- **Annotations as warnings:** a project site that stopped answering (kept for 3 days, then dropped), or a project
  sitemap that stopped parsing (the last good one is kept).
- **`site/sites.json` in git history:** every change to the list is a commit, so the history shows when a site
  appeared, failed or was dropped.

## Common fixes

- **A new project site doesn't appear:** it must be a public, non-fork repo with Pages on, answering 200 at
  `https://theguysaccount.github.io/<repo>/`. Run the workflow manually to pick it up immediately.
- **A project's sitemap is missing from robots.txt:** add a `Sitemap:` line to that project's own `robots.txt`
  (or publish `/sitemap.xml` at the project root). This repo reads it on the next run.

## Keys a human can turn later

- **Google Search Console** (needs a Google account): verify `https://theguysaccount.github.io/` with an HTML file.
  Add the file Google gives you to `site/` and push; the generator never deletes files it didn't write.
  Then submit `https://theguysaccount.github.io/sitemap.xml`. Google no longer accepts sitemap pings, so this is
  the only way to ask Google directly; until then it finds the sitemaps through `robots.txt`.
- **Bing Webmaster Tools:** optional. IndexNow already reaches Bing.
- **A custom domain:** don't add one here. A `CNAME` on the user site moves every project site without its own
  domain under that domain.
