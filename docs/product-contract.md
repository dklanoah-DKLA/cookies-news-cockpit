# Cookies News Cockpit — 1.1 product contract

## Product boundary

Cookies News Cockpit is a standalone, local-first macOS application. It does
not require Obsidian, the existing MarketDigest project, or a separately
installed Python runtime on the target Mac.

The first release targets one machine class:

- Intel (`x86_64`) MacBook Air (2019)
- macOS Sonoma 14 or newer
- 8 GB RAM

The application opens its cockpit in the user's default browser. Its service
binds only to `127.0.0.1` and exits after the cockpit has been inactive for five
minutes, unless a run is still finishing.

## Core workflow

1. A first-run guide selects source categories, creates a topic, optionally
   verifies DeepSeek, and starts the first run. Skipping source selection keeps
   four balanced, industry-neutral defaults.
2. The user creates up to 10 topics and chooses include/exclude keywords, a
   score threshold, an article limit, enabled sources, and a freshness window.
   Keyword separators are comma, Chinese comma, ideographic comma, semicolon,
   Chinese semicolon, CR, or LF; ordinary spaces remain part of a phrase.
3. The built-in catalog contains 16 categorized Chinese and English RSS feeds.
   A source stores its human homepage separately from its technical feed URL.
4. A manual run, or an hourly run while the cockpit is active, fetches and
   reviews duplicates before AI analysis. By default it compares the current
   candidates with the previous seven days across runs, sources, and topics.
   The same URL or a highly similar title is suppressed, while a materially
   updated title may appear again.
5. Feed metadata is filtered before optional article-page extraction. If a
   topic has no lexical hits, at most 20 newest, source-rotated candidates per
   run may enter the controlled DeepSeek semantic fallback. Version 1.1 uses
   `deepseek-v4-flash` with thinking disabled for these structured tasks. A hard
   technical breaker still stops after 500 normal analyses in a single run.
6. The latest successful report remains visible if a later run fails or is
   degraded. Partial source failures do not abort healthy sources.
7. The user can search history, favorite articles, cancel a run, export a v2
   backup, preview an import, and apply a transactional safe merge. The export
   never contains the DeepSeek key.

## Data and privacy

- Application data: `~/Library/Application Support/Cookies News Cockpit`
- Logs: `~/Library/Logs/Cookies News Cockpit`
- DeepSeek key: macOS Keychain
- Canonical run artifacts: immutable JSON under the application data folder
- Search index: rebuildable SQLite FTS index
- Full extracted article text and detailed logs: purged after 30 days
- Reports, favorites, configuration, and canonical metadata: retained

No telemetry is included in 1.1. Network calls are limited to user-enabled
sources and DeepSeek.

## Failure semantics

- A failed or degraded run never overwrites the last known-good report.
- Duplicate review is on by default, runs before DeepSeek analysis, and records
  the number of suppressed candidates in the run metadata.
- A finite freshness window excludes missing, invalid, or clearly future dates
  and reports those exclusions in the funnel.
- A source timeout is recorded against that source; remaining sources continue.
- 401/403 responses are not retried. A 429 honors `Retry-After`; timeouts, 5xx,
  malformed JSON, and schema failures receive at most one retry within one
  shared 45-second budget. If that still fails, the remaining candidates
  stop making AI calls and fall back to keyword screening without deleting
  fetched candidates.
- Topic calibration is a draft. It becomes active only after explicit user
  confirmation.
- The cloud-transfer scope and the 500-analysis safety breaker are disclosed
  before a key is saved. Actual cost remains the DeepSeek account holder's
  responsibility; 1.1 does not claim to calculate or enforce a spending limit.

## Local API contract

All endpoints are same-origin and require the runtime session token except the
initial static shell.

- `GET /api/bootstrap`
- `GET|PUT /api/settings`
- `PUT|DELETE /api/settings/deepseek-key`
- `POST /api/settings/deepseek/test`
- `GET|POST /api/topics`
- `PUT|DELETE /api/topics/{id}`
- `POST /api/topics/{id}/restore`
- `POST /api/topics/suggest`
- `POST /api/topics/{id}/calibrate`
- `POST /api/topics/{id}/calibrate/confirm`
- `GET|POST /api/sources`
- `PUT|DELETE /api/sources/{id}`
- `POST /api/sources/{id}/restore`
- `POST /api/sources/validate`
- `POST /api/sources/{id}/validate`
- `POST /api/runs`
- `GET /api/runs/current`
- `GET /api/runs/{id}`
- `POST /api/runs/{id}/cancel`
- `GET /api/reports/latest`
- `GET /api/history`
- `PUT /api/articles/{id}/favorite`
- `GET /api/export`
- `POST /api/import/preview`
- `POST /api/import/{id}/apply`
- `POST /api/heartbeat`
- `POST /api/shutdown`

Import preview and apply responses report source, topic, article, and run
counts separately. Run counts distinguish new, exact-match, and safely
remapped IDs. ZIP input is capped at 100 MiB compressed and 100 MiB
uncompressed; the DeepSeek key is never part of the archive.

## Distribution contract

GitHub Actions builds the application on an official Intel macOS runner with
Python 3.12 and `MACOSX_DEPLOYMENT_TARGET=14.0`. PyInstaller produces a windowed
`onedir` application bundle. The workflow applies ad-hoc integrity signing,
checks architecture/signature/package layout, launches the packaged executable
against a loopback smoke test, creates a DMG, emits SHA-256, and makes a ZIP
whose root contains exactly that one DMG. Tagged builds publish permanent
GitHub Release assets as well as the 30-day Actions artifact.

Version 1.1 is not Developer ID signed or notarized. The recipient may need to use
System Settings → Privacy & Security → Open Anyway on first launch. A future
Developer ID release can remove this friction without changing the data model.

## Release gates

- Backend unit and API tests pass without a real network or API key.
- The visual cockpit passes the Hallmark slop test and is inspected at 320, 375,
  414, 768, and a 13-inch desktop viewport.
- The app binds only to loopback and rejects invalid hosts/tokens.
- Export and logs contain no plaintext DeepSeek key.
- An actual Intel Sonoma Mac completes the WeChat → unzip/mount → install →
  first-launch acceptance path before the build is called production-ready.
