# Changelog

## Unreleased

- **Sync to speech (Whisper):** connect a speech-to-text service (Settings → Connections → Whisper; any whisper-asr-webservice-compatible server, or the small faster-whisper one in `deploy/whisper`). Each subtitle line is found in the transcript of the audio, and the timing follows the speech:
  - endings taken from another release (cut-off tracks) are moved onto the speech; Northwind S02's Bazarr endings were 1.5–4 s early because that release is edited differently right after the cut;
  - downloaded subtitles are synced to the speech before translating;
  - each translation's page has **Re-sync to speech**, which re-times the existing file without re-translating (free; the old timing stays in the editor's history).
  Matching is word by word, so music and effects don't fool it; a line whose words land in two places isn't trusted, outliers are ignored, and the track's own habit of how early it shows a line is kept, so a track that's already right is left alone.
- The line an embedded track stops on is kept when the ending comes from another file (it could be dropped when that file was timed earlier).

## 0.2.7 — 2026-11-16

- **Incomplete source tracks:** a built-in subtitle track that stops early (its last line stretched over the rest of the video, or nothing for the last quarter) is now detected. A complete subtitle file next to the video in the same language is used instead; otherwise Bazarr is asked for one and the job waits up to 3 hours, then translates what there is, shortens the stretched line and goes to Needs review with a note saying where the source stops. Any source line longer than a minute is shortened. Found on Northwind season 2 WEB-DL releases, whose English CC tracks end 5–14 minutes early.
- **Edge cases of that check:** a long last line only counts as a cut when it reaches the end of the video (a song over the credits doesn't make a job wait); a line stretched over a minute in the middle of a track sends the job to review, since dialogue may be missing there; a Batch result is applied to the exact source it was built from, so a subtitle arriving while the batch runs can't shift the line numbers; a video that can't be read (e.g. a network-share hiccup) fails with a readable message.
- **Downloaded subtitles are lined up before use:** the built-in track is kept for everything it covers (its timing is exact) and only the missing ending is taken from the downloaded subtitle, lined up on the lines both share in the 5 minutes before the cut (within 0.1 s on Northwind S02) (shift and stretch: a file made for 25 fps drifts by minutes otherwise, and Bazarr's sync only shifts). A file that doesn't line up closely is not used. The original file is never changed: the combined copy lives in the cache.

## 0.2.6 — 2026-11-16

- **Notes for removed series:** Settings → System can check for translator's notes, correction logs and per-series settings whose series or movie is no longer in Sonarr or Radarr, and delete them one by one. Nothing is deleted automatically (a series downloaded again reuses its notes). Nothing is listed or deleted while Sonarr or Radarr can't be reached, and each Delete checks again first.
- **Deleted videos:** when a series or movie is deleted on the server, its rows on the dashboard and its job pages are marked "Video deleted" and lose Edit, Retry, Download, Share and Re-translate. The rows stay so the month's spending still adds up. Nothing is marked while the library folder is unreachable (e.g. a network share that isn't mounted).

## 0.2.5 — 2026-11-16

- **Dashboard:** an older translation of a file that was translated again is marked "Replaced by a newer translation" and loses its Edit button. Its job page links to the newer job.
- **Job page:** Editor, Download, Share and Re-translate sit together in one row.
- **Costs:** estimates on the series and library pages show three decimals below $0.10 (e.g. $0.008).
- **OpenSubtitles upload fixed for episodes:** the title lookup now uses the episode's own IMDb ID (OpenSubtitles' `IMDBEpisode`) instead of the series' ID, which OpenSubtitles refuses for episode files. Uploading an episode under the series ID is caught with a clear message, and upload errors are logged.
- **Download:** the share page and each job's page can download the translation, under exactly its name on disk, to share it by hand.
- The share form credits the model that made the file, not the model selected now.
- **Docs:** re-translate, download, effort settings (`CAPTIONIZERR_OPENAI_EFFORT`, `CAPTIONIZERR_OLLAMA_EFFORT`) and automatic prices are documented. The README's safety section now says exactly which files can be replaced.
- **Cleanup:** removed unused imports and variables.

## 0.2.4 — 2026-11-16

- **Re-translate:**
  - A button on a job's page, and the series page can now select already-translated episodes.
  - The file (and its forced track) is replaced. The previous version stays in the editor's history.
  - Retrying a "needs review" job may now replace its own file; before, it was skipped as "already exists".
- **Effort for other providers:**
  - OpenAI-compatible gets a reasoning effort setting (model default / minimal / low / medium / high). It's dropped, with a note, if a model rejects it.
  - Ollama gets thinking off / low / medium / high, falling back to plain on/off or none for models without levels.
- **API key fields:**
  - Aligned with the other fields.
  - They say **✓ Saved** or **Not set**, with the key's last four characters and the date it was saved.

## 0.2.3 — 2026-11-16

- **Web UI:**
  - The name is shown as **CaptionizeRR** on the web pages and the website.
  - The Translations list has a **Model** column, and each job's page names the model that did the work.
  - Long job notes are shortened to two lines; the full note shows on hover and on the job page.
  - Sub-dime costs show three decimals (e.g. $0.008 per episode).
  - System → "Changed here" uses readable setting names.
- **CLI:** `captionizerr translate` now requires `--lang` instead of silently defaulting to Russian.
- Examples on the website, in the README and in the UI no longer assume Russian.
- **Review accuracy:**
  - Short ALL-CAPS on-screen text such as `TEST SUBJECT: AI COLEMAN` is no longer mistaken for a speaker label.
  - Style-only issues (a leftover speaker label, lost italics) still get a fix attempt, but no longer put a whole job into "needs review".
  - The editor's flags now come from the pipeline's own validator, with the same tolerances, so the editor and the job status never disagree.
  - A job's detail names its open lines ("open: #296 …"), and **Review** opens the editor on the flagged lines.
- `deploy/deploy.sh` waits for running translations before restarting (`--force` to skip).
- **OpenAI-compatible providers:**
  - Uses `max_completion_tokens` where required (newer OpenAI models such as `gpt-6-luna`), adapting to whichever parameter a server names, and drops an unsupported `temperature`.
  - API errors are shown readably.
  - The Base URL is prefilled with OpenAI's address.
- **Automatic prices:**
  - **Find models** lists your provider's models.
  - Picking one (or pressing **Test**) fills in its price and shows the cost per episode.
  - Sources: the built-in table (Anthropic), OpenRouter's live prices, the LiteLLM community price list, and $0 for local servers. They're cached for 24 h and used offline when stale.
  - A warning appears when $0 prices would leave the budget cap blind.
  - The built-in Anthropic prices are cross-checked against the published list.
- OpenSubtitles login errors explain the one-time opensubtitles.org sign-in.
- **Switching vendors or models keeps costs correct:**
  - Each job records its model. "Per episode" estimates use the current model's own history, otherwise its price; before, they averaged across models.
  - Editor line re-translations are priced by the model that did them; before, they were always priced as the Anthropic model.
  - An open backlog batch is still collected after switching away from Anthropic.
  - An unknown price is stated in the job's detail instead of silently counting $0, and dated snapshot model names are priced.
  - The CLI uses the same keys (including keys saved in the UI) and price lookup as the service.

## 0.2.2

Documentation release: **the application is unchanged from 0.2.1** apart from the version number. No need to upgrade for fixes.

- **Project website:** https://captionizerr.github.io/captionizerr/ (published from `site/` by GitHub Pages). It includes:
  - a side-by-side example, with Captionizerr's real output for an original scene next to an illustrative literal translation;
  - architecture diagrams, a UI tour, features, measured cost and quality, quick start, security and a FAQ.
- The README and the repository homepage link to the website.

## 0.2.1

Security and documentation fixes from a full review. Upgrading is recommended; nothing to change.

- **Fixed (security):**
  - A crafted editor request could use a series name to read `.md` files outside `config/context/` (sent to the AI as notes) or append to files outside `config/feedback/`. Series keys are now checked wherever they become file names.
  - "Back"/"next" links accepted `/\host`, which browsers treat as another site (open redirect). They now accept local paths only, including in "Translate selected".
- The image no longer carries pip build leftovers. `.dockerignore` and `.gitignore` keep `.env`, `secrets/`, `config/` and databases out of builds and commits.
- **Docs:**
  - The configuration page wrongly said secrets can only be set in the environment.
  - SECURITY.md now describes the current protections and recommendations.
  - The README reports the measured local-model comparison.

## 0.2.0

**Set everything up in the browser.** A fresh install opens a setup wizard (login, AI provider and key, languages and budget, Sonarr/Radarr, media server); no config file is needed any more.

- **Settings** are split into General, AI provider, Connections, Security and System. The separate "AI providers" page merged in; old URLs redirect.
- **Keys and passwords in the UI:**
  - stored in `config/secrets.json` (mode 600), never in the database, never shown back (write-only fields);
  - the UI password is a salted PBKDF2 hash;
  - secrets from files (`*_FILE`) or the environment still win, and show as "Set on the server".
- **Test button for every connection** (Anthropic, Ollama, OpenAI-compatible, Sonarr, Radarr, Jellyfin/Emby, Plex, Bazarr, ntfy, OpenSubtitles), all read-only or free.
- **One-click webhooks:** "Set up webhook" creates or updates Captionizerr's webhook in Sonarr/Radarr (Basic auth; import, upgrade and rename) and confirms with the app's test event. Making a new webhook key updates connected webhooks automatically.
- **Security:**
  - every secret accepts `<NAME>_FILE` (Docker/compose secrets);
  - webhooks authenticate with HTTP Basic (the key as the password) instead of a key in the URL; `?apikey=` still works;
  - 10 failed logins or keys lock an address out for 15 minutes;
  - SECURITY.md has a Caddy HTTPS recipe.
- **Dashboard:** "Needs attention", "In progress" and budget at a glance; the list shows translations, with filters and an Edit button; editor and notes costs sit under "Other spend".
- **Library:** search, an "only with untranslated" filter, and translated progress per series.
- **Editor:** compact rows with ↻ per line; lines without dialogue hidden by default; dropped subtitle-site credits no longer count as flagged.
- **Fixed:**
  - "Set up webhook", line re-translation and OpenSubtitles upload no longer block the server (the webhook setup deadlocked waiting for Sonarr's callback);
  - the example `docker-compose.yml` had invalid YAML;
  - first-run setup could replace a webhook key passed in directly.

**Upgrading from 0.1.0:**
- Nothing to do: existing environment variables and settings keep working, and an already configured instance skips the wizard.
- Optionally move your webhooks to the new auth with **Settings → Connections → Re-create webhook**.

## 0.1.0 (first public release)

- Sonarr/Radarr webhooks, job queue, monthly budget cap with estimates, ntfy notifications, Jellyfin rescan, Prometheus metrics.
- Translation pipeline: whole-episode requests with prompt caching, validation (reading speed, length, leftovers, line boundaries, tags), targeted fix rounds.
- Sources: embedded text tracks, sidecar files, and a Bazarr fallback (download + sync); any source language.
- SRT and ASS (styles, signs, drawings and karaoke preserved).
- Web UI: dashboard, library (translate existing episodes and films), series settings and notes, side-by-side editor with per-line re-translate, settings, providers, storage and cleanup.
- Providers: Anthropic (with Batch API backlog at half price), Ollama (local), OpenAI-compatible.
- Optional login for the web UI; daily housekeeping including orphan cleanup and a daily DB snapshot.
- Upgrades re-time the previous translation (keeping edits) instead of re-translating; forced tracks get `.forced` translations.
- Open backlog batches are reserved against the budget; the editor opens without blocking while a source track is extracted.
- Plex and Emby rescans; the Settings page shows overrides of environment values with reset.
- Opt-in "Share on OpenSubtitles" (AI-translation flag, duplicate check, review confirmation).
- Validator flags leftover speaker labels and lost italics.
- `captionizerr eval` / `eval-build` quality check-set (chrF), `backfill-sources`, `--provider` for the CLI.
