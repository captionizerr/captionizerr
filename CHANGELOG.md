# Changelog

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
- `captionizerr eval` / `eval-build` quality check-set (chrF), `backfill-sources`, `--provider` for the CLI.
