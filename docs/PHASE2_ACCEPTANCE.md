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

## Cross-source audit (2026-11-03, after acceptance)

You asked for a thorough recheck. I compared every browser visit against the window record, read both terminal tabs, and checked the process tree. What I found and changed:

| Finding | Change |
|---|---|
| Active time disagreed with the window record (100 s vs about 59 s; 11 s vs about 84 s). Chrome's focus events are unreliable on Windows. | Active time now comes from the window record (ADR-033). `personal-os browser recompute-dwell` brought 17 stored visits in line. |
| `events` showed how long a tab stayed open (1046 s), not active time, and showed search URLs instead of queries. | Page visits show active time ("+" while open). Searches and selections show their text; hidden visits show the site. |
| The daemon ran inside a visible Windows Terminal window. Closing it killed capture. | Windowless start (ADR-034). Verified: two `pythonw.exe` processes, no console. |
| A Gmail visit stayed "open" forever after the extension was reloaded. | Browser visits that stop checking in for 3 minutes are closed at their last checkpoint. |
| The password-page counter grew by one per minute for a single open tab; a visit stored before its password field appeared was never removed. | Count once per visit; retract the stored row (ADR-035). |
| Every daemon restart re-recorded all 26 repos' git state. | Unchanged repos aren't re-recorded. Verified: second restart added 0 rows. |
| Deleting a build folder (`dist`) showed as a file change. | Ignored, like the folder's contents. |
| One terminal tab recorded nothing. | Not a bug: that PowerShell started before the hook was installed. A shell only loads its profile when it starts. Open a new one. |
| A search for a word from an email returned nothing. | By design: mail is kept as site and time only. |
| Follow-up: visits could vanish from `events --source browser`. If a page's title changed mid-visit (an inbox counter, a renamed doc), the window record matched only the last few seconds. The visit was then judged a bounce and its stored row deleted. Reproduced in a test: 90 s visit → 2 s. | The window figure isn't trusted when it is bounce-sized but the extension saw more, and all titles a visit had are matched (ADR-036). `personal-os browser recompute-dwell` restores visits an earlier run shrank. Visits already deleted can't be recovered. |
