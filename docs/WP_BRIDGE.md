# WordPress bridge — API contract

The bot exposes a small authenticated HTTP API so a WordPress plugin can ask
it to resolve an xdmovies page and receive structured link data back.

```
WP admin ──► app.py (Render) ──► bot API :8090 ──► plugin ──► /tgmovies/v1/auto-post
             holds the API key      does the scraping    shortens,
             so WP never sees it                        groups, posts
```

The bot never talks to WordPress, and the plugin never runs a browser.

---

## Enabling it

The API is **off unless `XDMOVIES_API_KEY` is set**. With no key the bot logs
`wordpress bridge API disabled` and nothing listens on the port.

```bash
# generate a real key -- do not hand-write one
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add to `xdmovies/.env` then restart:

```
XDMOVIES_API_KEY=<generated>
XDMOVIES_API_HOST=0.0.0.0
XDMOVIES_API_PORT=8090
```

`deploy.sh` opens the firewall port **only** when the key is present, so an
unconfigured deploy leaves it closed.

| Variable | Default | Meaning |
|---|---|---|
| `XDMOVIES_API_KEY` | *(empty)* | Required. Static shared secret, `X-Api-Key` header |
| `XDMOVIES_API_HOST` | `0.0.0.0` | Bind address |
| `XDMOVIES_API_PORT` | `8090` | Port |
| `XDMOVIES_API_MAX_CARDS` | `200` | Hard cap on cards per request; `0` = unlimited |
| `XDMOVIES_API_DEDUP_SECONDS` | `86400` | Resending a URL inside this window returns the cached result; `0` disables |
| `XDMOVIES_API_RATE_LIMIT` | `60` | Requests/min across the whole API |
| `XDMOVIES_API_NOTIFY_OWNER` | `0` | `1` also DMs the owner when a job parks on `needs_human` |

Check it with `/status` — it shows `wp-bridge: ON :8090 queued=0 store={...}`.

---

## Endpoints

All requests need `X-Api-Key: <key>`. `GET /api/v1/health` is the only
unauthenticated route and returns just `{"ok": true}`.

### `POST /api/v1/jobs`

```json
{ "url": "https://top.xdmovies.wtf/movies/baby-do-die-do-2160p-1080p-hindi-download-1512084",
  "card_filter": "all",
  "force": false }
```

`url` must be an `https://xdmovies.wtf` / `top.xdmovies.wtf` **detail** page —
`/movies/<slug>` or `/series/<slug>`. Anything else is `400`. This allowlist is
deliberate: without it the endpoint is an open proxy that will drive the VPS
browser at arbitrary sites.

`card_filter` is `all` | `episodes` | `packs` and reuses the same engine as
`/search` accept-all. It only narrows series episode/pack sections; movie cards
pass through every filter.

Returns `202` **immediately** — a series can take 20+ minutes and WordPress
times out long before that, so never block on this call:

```json
{ "job_id": "c06b9c3454c0", "status": "queued",
  "source_url": "https://top.xdmovies.wtf/movies/baby-...-1512084",
  "poll_url": "/api/v1/jobs/c06b9c3454c0", "poll_after_s": 5 }
```

If the same URL completed within the dedup window, returns `200` with
`"deduplicated": true` and the previous `result` instead of a new job. Send
`"force": true` to override.

### `GET /api/v1/jobs/{job_id}`

```json
{ "job_id": "c06b9c3454c0",
  "status": "running",
  "source_url": "https://top.xdmovies.wtf/movies/baby-...-1512084",
  "created_at": 1758600000.0, "started_at": 1758600002.1, "finished_at": null,
  "elapsed_s": 41.3,
  "solve_url": null, "error": null, "cancel_requested": false,
  "result": null }
```

When `status == "done"`, `result` is populated:

```json
{ "title": "Baby Do Die Do",
  "tmdb_id": "1512084",
  "language": "Hindi, Tamil",
  "meta": { "rating": "7.4", "genres": "Comedy, Crime",
            "first_air": "2026", "sources": "WEB-DL" },
  "cards": [
    { "index": 1,
      "raw_title": "Baby.Do.Die.Do.2026.2160p.NF.WEB-DL.DDP5.1.Atmos.H.265-XDMovies.com.mkv",
      "size": "13.02 GB",
      "language": "",
      "url": "https://hubcloud.ist/drive/ky6k6srirmhycr",
      "status": "resolved", "error": null }
  ],
  "stats": { "total": 4, "resolved": 4, "failed": 0,
             "elapsed_s": 81.2, "truncated": false } }
```

Use `tmdb_id` for `movie_name` when present, else `title` — that is what the
plugin's TMDB importer keys off.

`url` is the **final, unshortened** destination. Shortening, quality grouping
and the `<b>QUALITY</b>` pipe formatting are all the plugin's job.

### `DELETE /api/v1/jobs/{job_id}`

Marks the job cancelled. Checked at every card boundary, so it stops within one
card. `409` if the job already reached a terminal state.

### `GET /api/v1/jobs?status=done&limit=50`

Listing for debugging. Returns `{jobs: [...], count, depths: {...}}`.

---

## Status machine

```
queued ──► running ──► done
                  ├──► failed
                  ├──► cancelled
                  └──► needs_human ──► queued     (after a successful solve)
```

`needs_human` is a **parked** state. The bot has already released its worker
slot, so a blocked WordPress job never stalls Telegram traffic. A background
waiter owns the solve session; when the cookie is captured the job re-enters
`queued` automatically and carries on. If nobody taps before the solve session
expires, the job goes to `failed` with a clear message.

While parked, `solve_url` holds the link to show the operator. The plugin
should surface it in wp-admin — the bot sends **no** Telegram message on this
path unless `XDMOVIES_API_NOTIFY_OWNER=1`.

Poll interval: 5s while `queued`/`running`, 15s while `needs_human`, stop at
`done` / `failed` / `cancelled`.

---

## Priority — Telegram always wins

API jobs occupy a third, lowest-priority queue lane:

```
urgent ──► normal (Telegram links) ──► api (WordPress)
```

A running API job also pauses at **every card boundary** while interactive work
is waiting, so a bulk WordPress import never makes your own `/queue` feel
stuck. Cancellation of API jobs is deliberately *not* reachable from `/cancel` —
use `DELETE` so cancelling your Telegram queue cannot kill a scrape.

---

## Failure handling

| `status` | `error` contains | What to do |
|---|---|---|
| `failed` | `extract: ...` | Scrape blew up. Log it, allow a manual retry. |
| `failed` | `no download links found on page` | Bad/removed slug. Do not retry. |
| `failed` | `solve timed out; ...` | Nobody tapped the checkbox. Resubmit for a fresh link. |
| `failed` | `bot restarted while job was running` | Interrupted by a deploy. Safe to retry. |
| `done` with `stats.failed > 0` | — | Partial success. Post the resolved cards anyway. |
| `done` with `stats.truncated = true` | — | Hit `XDMOVIES_API_MAX_CARDS`. Some cards were skipped. |

Interrupted jobs are swept to `failed` on the next boot, so the plugin never
polls a `running` job forever.

---

## `app.py` on Render

A thin relay — it holds the bot key so WordPress never sees it.

```python
BOT_BASE   = os.environ["BOT_BASE"]     # http://<vps>:8090
BOT_KEY    = os.environ["BOT_KEY"]      # XDMOVIES_API_KEY
PLUGIN_KEY = os.environ["PLUGIN_KEY"]   # shared secret from the plugin

# POST /xdm-scrape      {"url": ..., "card_filter": ..., "force": ...}
#   -> forwards to BOT_BASE/api/v1/jobs, returns 202 + job_id
# GET  /xdm-result/<job_id>
#   -> forwards to BOT_BASE/api/v1/jobs/<job_id>, returns the poll payload
# DELETE /xdm-cancel/<job_id>
#   -> forwards DELETE to the bot
```

Use `requests` with a 15s timeout. Both services are I/O-bound, so threads
over async are fine at this volume. Give Render a health check route so it
does not sleep mid-import.

---

## Plugin side

1. Post-meta box: **"Send to xdmovies resolver"** → stores `job_id` in
   `_xdm_job_id` post meta.
2. WP-Cron (30s) polls `/xdm-result/<job_id>` for posts that still have a
   `job_id` and no terminal marker.
3. On `needs_human`, render an admin notice with the `solve_url` — do not
   resend the request, the bot resumes by itself once solved.
4. On `done`, build your existing pipe rows from `result.cards` and call the
   route you already have:

```
<b>2160P</b>
2160P NF WEB-DL DDP5.1 H.265 HINDI, TAMIL [HUBCLOUD]|<shortened>|<size>
<b>1080P</b>
...
```

   Derive the quality/host/source tags from `raw_title` + `url` (port
   `extract_quality` / `identify_host` / `extract_source_tag` from
   `4kbolly.py`), shorten via cinealert, then `POST /tgmovies/v1/auto-post`
   with `merge_links: true`. If it answers `already imported`, call
   `/tgmovies/v1/append-links` instead.
5. Mark the post meta done so cron stops polling.

The plugin is responsible for sanitising `raw_title` and `url` before they go
into `post_content` — they come from a scraped page and must be escaped on
output.

---

## Security checklist

- [ ] `XDMOVIES_API_KEY` is 32+ random bytes from `secrets.token_urlsafe`
- [ ] Key lives only in the VPS `.env` and Render's secrets — never in the
      plugin source, never in git
- [ ] `XDMOVIES_API_NOTIFY_OWNER=0` so solve links stay out of Telegram
- [ ] Terminate TLS at Render; the bot speaks plain HTTP on the VPS
- [ ] The WP `secret_key` is a real random value compared with `hash_equals()`,
      not a hardcoded literal
- [ ] Any Telegram tokens in `4kbolly.py` have been rotated
