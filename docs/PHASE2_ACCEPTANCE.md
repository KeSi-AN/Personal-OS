# Phase 2 acceptance

**Brief's test:** "Browse a research session; stored context is readable and useful, not a URL dump."
**Run:** 2026-11-03, two short sessions in Chrome on the real data dir, about 20 minutes total, reviewed after each. No captured content is reproduced here, because this repo is public.

## Result: accepted

The second session, after the first round of fixes, shows what this phase is for. A search led to a technical article read for about 1.5 minutes and scrolled 82%. It was stored with:

- **A clean extract:** about 2,600 words of main text and its section headings.
- **The search that led to it**, recorded with its query.
- **The sentence you selected and copied** from it, linked into the same visit chain.

That's research context, not a URL list.

| Check | Result |
|---|---|
| Searches recorded with their queries | **Pass.** 8 searches across Google and YouTube. |
| Visit chains (search → result → follow-on pages) | **Pass.** |
| Readable page text | **Pass after fixes.** Article pages give full extracts with headings. |
| Active time = time actually looking at the page | **Pass after fixes** (see below). |
| Content types and site details (YouTube video id, GitHub repo, docs, mail, documents) | **Pass.** |
| Selection and copy captured, never from form fields | **Pass.** |
| Webmail kept as domain + time only | **Pass.** Title hidden, no text. |
| Your own documents (Google Docs/Drive etc.) kept without text | **Pass after fixes.** |
| Bounces under 5 s dropped unless searched/bookmarked | **Pass.** 6 dropped. |
| Secrets and email addresses redacted | **Pass.** Token-like URL parts and email addresses redacted. |
| Native host, pause, incognito and blocked-site handling | Covered by tests (host protocol in a real subprocess; the privacy filter end to end). Incognito can't be enabled for the extension at all. |

## Fixes made from the review

**Round 1** (after the first session):

1. **Active time was 0 s for pages opened in the tab already in front.** The worker only learned the front tab from tab switches; it now learns it from the navigation itself, and reads the real focus state when it starts.
2. **Weak page text.** On some sites Readability picked a footer. A footer-sized result now loses to the page's main area, single-page-app views are read sooner, and a page that hasn't been read yet is read when you switch away from it.
3. **A Google Doc's text included the account email from the Docs toolbar.** Your own documents (Google Docs/Drive, OneDrive, SharePoint, Notion) are now kept with title and time only (`privacy.no_text_domains`); email addresses are redacted from all free text; and the text already stored was deleted (verified absent from the database file).
4. **Search URLs kept clutter** (`oq`, `gs_lcrp`, …). They now keep only the query.
5. **Titles began with notification counters** like "(3995) ". These are now stripped.

Also: settings added in a newer version now fill in from the defaults for existing `config.toml` files. Without this, fix 3 wouldn't have applied on a machine set up earlier.

**Round 2** (after the second session; verified by tests, not yet in a live session):

6. **Address-bar navigations still showed 0 s active.** Chrome pre-renders them in a hidden tab and swaps it in under a new id; the worker now follows `tabs.onReplaced`.
7. **YouTube videos got no page text.** The page rewrites its URL after loading, so the text report didn't match the visit. Reports now match on origin, path and video id, and single-page-app views are read twice (once rendered, once settled).
8. **A chat widget's flashing title** ("Message from …") replaced a page's real title. The real title is now kept.

## Not exercised live

Brave (same extension; its native-host key is registered by `personal-os browser install`), bookmarks, and a page with a password field (covered by tests).
