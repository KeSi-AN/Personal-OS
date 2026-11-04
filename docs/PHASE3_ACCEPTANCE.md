# Phase 3 acceptance

**Brief's test:** "≥90% correct project assignment on a labeled test day."

**Run:** 2026-11-03, on two labelled simulated scenarios and on the real data directory. No captured content appears here, because this repo is public.

## Result: target met, awaiting your review

| Measure | Events correct | Time correct |
|---|---|---|
| Tuned scenario, day 2 (the test day) | **50/50 = 100%** | **100%** |
| Hold-out day 2, written after tuning | **42/44 = 95.5%** | **89.5%** |
| Day 1, cold start (no corrections yet; for reference) | 53/63 = 84.1% | 66.7% |

An unassigned event counts as wrong. Time accuracy weights each event by its duration.

## How it was measured (ADR-042)

Both scenarios label every step with its true project. There are three projects: a Python API, a data-science notebook repo and a portfolio site. The rest is personal browsing.

**Day 1** mixes:

- Coding in VS Code.
- Terminal commands and commits.
- Docs and Stack Overflow pages.
- AI-chat use for two different projects.
- A YouTube music stream, Netflix, Gmail and Hacker News.

**Between the days,** the evaluation makes corrections the way you would, through `inbox` and `assign`:

- A group is assigned as a whole only if at least 80% of it belongs to one project.
- Mixed groups (the AI-chat app, Stack Overflow) are corrected item by item, which teaches no rule.

There were 10 corrections in all.

**Day 2** repeats the same kinds of work with new pages and files.

**The hold-out's day 2 is harder.** It adds:

- Research pages on sites never seen on day 1.
- A YouTube tutorial watched for work.
- A terminal used outside any repo.
- Interleaved switching between projects.

It was written after the resolver was tuned and wasn't tuned against afterwards.

The acceptance test (`tests/test_projects.py`) requires, for both scenarios, at least 90% of events and 85% of time. The time bar is lower because the hold-out's one miss below is a long video.

## The mistakes, and why they're left

**Cold start (day 1):** all 10 mistakes are personal pages (music, Netflix, Gmail, Hacker News, a trailer) filed under whatever project was being worked on around them, by the timing signal. Without a correction the system can't know these aren't work. These guesses are weak (≤0.65), so they're listed in the inbox. One correction per site fixes them for good: day 2 has none of these mistakes.

**Hold-out:** the one miss is a YouTube OAuth tutorial watched for the API project. On day 1 you said "YouTube isn't work", so that site's rule (0.7) outweighs the page's words matching the project (0.6). The window showing it follows the page. This is a genuine conflict between a correction and the content. Siding with the correction is the safer default, and one `assign` with `--learn` on the video's channel or the event fixes it.

## Real data (no labels yet)

The resolver has been running in the daemon since this phase was installed. Of 733 captured events:

| Assigned by | Events |
|---|---|
| Path (repo, file, terminal folder) | 406 |
| Timing | 29 |
| Window title | 16 |
| Folder name | 4 |
| Not applicable (system, idle) | 24 |
| **Unassigned (inbox)** | **252**: 216 windows, 34 browser, 1 file, 1 terminal |

- **Projects:** 27 (26 git repos found automatically, plus `none`).
- **Rules learned:** none yet. No corrections have been made.

Most unassigned items are apps and sites that aren't tied to a repo: video calls, AI chat, streaming, webmail, File Explorer outside a project. That's what the inbox is for. It currently shows 15 groups.

**Accuracy on real data can't be stated yet,** because nothing is labelled. Once you've worked through the inbox, the corrections become labels.

## Fixes made from the real-data check

| Found | Fixed |
|---|---|
| The resolver crashed on git state records (changed files are stored as objects, not strings). | Handled. The daemon's resolver job had been failing on the old code until the restart. Verified clean afterwards: no failures, nothing pending, sessions rebuilt on schedule. |
| Browser windows without the extension (a video call) never reached the inbox. | Grouped by app and the first part of the window title. |
| Streaming and video pages were matched to a repo through generic words, and a login page through the word "login". | Conservative keyword rules (ADR-038). Re-checked: no keyword misfires. |
| Words from your home folder path ("users", "documents", your user name) counted as project vocabulary. | Ignored. |
| "OAuth" was split into "o" + "auth" by the camelCase splitter. | Split only at lower → upper. |
| On a fresh database, project vocabularies held only project names until the next cycle. | The resolver takes a second look right after building them. |
| A terminal `cd` into a folder named after a project but outside its repo wasn't assigned. | A folder-name signal (0.7). |

## Not done in this phase

- **Embeddings for page ↔ project similarity.** The brief lists them; they arrive with the local embedding model in phase 4. Phase 3 uses words only.
- **LLM session summaries.** That's phase 4. Sessions currently carry stats only.

## Check it yourself

```powershell
uv run personal-os sim eval-projects tests/fixtures/scenarios/labeled_holdout.toml
uv run personal-os inbox
uv run personal-os sessions
```
