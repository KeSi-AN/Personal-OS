# Phase 2 demo: browser context

Phase 2 adds a Manifest V3 extension for Chrome and Brave (Edge works too). For each page you actually read, it sends a compact, readable extract: title, headings, meta description, the first 3,000 characters of main text, and language. It also sends how long the page had your attention, scroll depth, video watch time, how you got there, and text you select or copy. The daemon works out what kind of page it was and adds details for GitHub, Stack Overflow, docs sites and YouTube.

Never sent: raw HTML, form-field contents, anything from a page with a password or payment field, incognito/private windows (the extension can't even be enabled there), and pages on blocked sites. Visits under 5 seconds are dropped unless you searched for or bookmarked the page.

## 1. Tests

```powershell
uv run pytest -q                       # includes 36 browser tests (enrichment, host protocol, privacy)
cd extensions/browser; npm test; cd ../..   # extension dwell/chain/privacy logic
```

## 2. Build the extension

```powershell
cd extensions/browser
npm install
npm run build        # writes extensions/browser/dist
cd ../..
```

## 3. Register the native host and turn browser capture on

```powershell
uv run personal-os browser install            # Chrome, Brave and Edge (or --browser chrome)
uv run personal-os daemon stop; uv run personal-os daemon start
```

This writes a host manifest and launcher into `%LOCALAPPDATA%\PersonalOS\native_host` and adds one per-user registry key per browser. No admin rights are needed. To undo it: `uv run personal-os browser uninstall`.

## 4. Load the extension (Chrome first)

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick `extensions\browser\dist`.
3. Check that the ID is `oapjpjppogicpgagjemcbpedbbjajmap`. The native host only answers that ID.
4. If the toolbar icon shows **!**, the host isn't reachable: check step 3. **II** means capture is paused.

For Brave, do the same at `brave://extensions`.

## 5. Browse, then look

Do a short research session: search for something, open a few results, read a docs page and a Stack Overflow answer, watch part of a YouTube video, copy a snippet. Then:

```powershell
uv run personal-os events --source browser --last 30
uv run personal-os search "words from a page you read"
uv run personal-os browser status
uv run python scripts/review_capture.py --minutes 30
```

Things to check:

- Each page you read is one `browser.page_visit` with a sensible duration (only the time the tab was in front of you and you weren't idle).
- Searches show as `browser.search` with the query.
- Stack Overflow, GitHub, docs and YouTube pages have the right type and details (`events --json` shows `content_type` and `enrichment`).
- Text from pages you read is findable with `search`.
- WhatsApp, Instagram, banking and login pages don't appear. Gmail appears only as "mail.google.com" plus time spent.
- Nothing from an incognito window appears.

## Acceptance (brief §10)

> Browse a research session; stored context is readable and useful, not a URL dump.
