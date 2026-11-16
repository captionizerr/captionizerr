# Changelog

## Unreleased

- **CLI:** `captionizerr translate` now requires `--lang` instead of silently defaulting to Russian.
- Examples on the website, in the README and in the UI no longer assume Russian.

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
