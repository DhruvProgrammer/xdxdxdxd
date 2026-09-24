=== xDMovies Resolver Bridge
Contributors: xDMovies
Tags: movies, telegram, resolver, tmdb, download links
Requires at least: 6.0
Tested up to: 6.5
Requires PHP: 7.4
Stable tag: 1.0.0
License: GPL-2.0-or-later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Send an xdmovies page to the resolver bot, then post the resolved links to
tgmovies through the site's own TMDB importer.

== Description ==

This plugin is the WordPress half of a three-part bridge:

    WordPress  ->  Render (app.py)  ->  xdmovies bot  ->  back to WordPress

The WordPress site never holds the bot API key. The Render bridge holds it,
forwards the request, and returns the result. This plugin then shortens the
links and calls the site's existing REST routes:

* `/?rest_route=/tgmovies/v1/auto-post`
* `/?rest_route=/tgmovies/v1/append-links`

No browser runs on WordPress and the bot never talks to WordPress directly.

= Features =

* "Resolve with bot" button in the post editor.
* Content filter: all links, episodes only, or packs/zips only.
* Background polling via WP-Cron (one-minute schedule) plus an opportunistic
  poll whenever an admin page loads.
* Cloudflare "needs human" handling: shows a solve link in the admin; the bot
  resumes by itself once the checkbox is tapped.
* Automatic fallback to `append-links` when the movie already exists on the site.
* Per-job history screen with status, errors, and a link to the resulting post.

== Setup ==

1. Copy the `xdmovies-bridge` folder into `wp-content/plugins/`.
2. Activate "xDMovies Resolver Bridge".
3. Go to **xDMovies Bridge** in the admin menu and fill in:
   * Bridge URL (your Render `app.py` URL)
   * Bridge Key
   * tgmovies Site URL
   * tgmovies `secret_key`
   * Shortener endpoint (the encoded link is appended to this value)
4. Open any post, paste an `https://…xdmovies.wtf/movies/…` or `/series/…`
   URL into the **xDMovies Resolver** box, and click **Resolve with bot**.
5. Watch progress on the Bridge screen.

== Frequently Asked Questions ==

= Why does the job say "Cloudflare needs you"? =

The site asked for a human verification. The bot parks the job, frees its
worker so nothing else stalls, and waits. Tap the solve link; the bot captures
the cookie and continues on its own. You do not need to resend the request.

= Why is my link unshortened? =

That is expected when the shortener endpoint is empty or unreachable — the
plugin keeps the original URL rather than dropping the link.

= Can I cancel a job? =

Yes, from the Bridge jobs screen. Cancelling only affects WordPress-initiated
jobs; your Telegram bot queue is unaffected.

== Changelog ==

= 1.0.0 =
* Initial release: bridge client, job poller, link formatter, importer writer.
