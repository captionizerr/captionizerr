<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/mark-white.svg">
    <img src="assets/brand/mark-black.svg" alt="Captionizerr" width="120">
  </picture>
</p>

<h1 align="center">Captionizerr</h1>

<p align="center">LLM subtitle translation for the *arr stack, timed to the file and faithful to the original.</p>

Captionizerr watches Sonarr and Radarr. When an episode or film is imported, it takes the subtitle track that ships with that file and translates it with an LLM. The translation is written next to the video as `<video>.captionizerr.<lang>.srt`, where your media server picks it up as an extra subtitle track. Tested with Jellyfin. Emby (same API) and Plex rescans are implemented but not yet tested against real servers.

Because it translates the file's own track, the timing is exact for that release. Downloaded subtitles are often from another cut and drift. Because the model sees the whole episode at once, it knows who is speaking to whom. That gives correct grammatical gender and formal/informal address, jokes that land, swearing as strong as the original (or softened, your choice), and songs translated as rhymed verse.

> **Status: 0.1, early.** It has run against a real library for a while, but expect rough edges. Issues and PRs are welcome.

## Features

- **Automatic:** Sonarr/Radarr webhooks queue every import, and renames are followed.
- **Upgrades are re-timed, not re-translated:** when a better release replaces a file, the previous translation (including your edits) is matched line by line onto the new release. Only new or changed lines go to the model, usually at $0.
- **Any source language:** embedded text tracks first (exact timing), then subtitle files next to the video, then **Bazarr**. If a video has no usable subtitle, Captionizerr asks Bazarr to download and sync one, then translates it.
- **SRT and ASS:** for anime, styles, signs, drawings and karaoke survive.
- **Forced subtitles:** foreign-language scenes and signs (a forced track in the source) get their own `….<lang>.forced.srt`, which players show automatically.
- **Quality checks:** every line is checked for reading speed, line length, untranslated leftovers, merged or shifted lines and lost positioning tags. Only flagged lines are sent back for a fix.
- **Series notes:** a short "bible" of characters with gender and formality, a glossary and tone. It's drafted automatically from the first episode and editable, and every episode of the series uses it, so names stay consistent.
- **Editor:** source and translation side by side, with reading-speed flags and per-line re-translate ("make it more casual"). Your corrections can be turned into series notes.
- **Share on OpenSubtitles (opt-in):** after reviewing a file in the editor, upload it to OpenSubtitles.org with your account. It's always marked as an AI translation and never shared automatically.
- **Cost control:** an estimate before every job, a monthly cap (jobs pause and you're notified), and a **backlog at half price** through the Anthropic Batch API.
- **Providers:** Anthropic (reference), **local models via Ollama** (free, private), or any OpenAI-compatible API (OpenAI, OpenRouter, DeepSeek, vLLM, LM Studio).
- **Ops:** Jellyfin, Emby and Plex rescans, ntfy notifications with a spot-check sample, Prometheus `/metrics`, daily housekeeping and a daily database snapshot.
- **Quality check-set:** `captionizerr eval` re-translates excerpts you approved and scores them, so prompt or model changes are measured rather than guessed.

## Safety

Captionizerr writes into your media library, so the writer is deliberately narrow:

- It only ever **creates** files named `<video>.captionizerr.<lang>.srt|ass`, and only next to videos under `CAPTIONIZERR_LIBRARY_ROOTS`.
- It never overwrites (exclusive create) and never touches any other file.
- The editor can replace only its own files, and keeps the previous version outside the library.
- Videos are opened read-only. ffmpeg writes only to a private temp folder, with `-n` (no overwrite).
- The daily cleanup moves Captionizerr's own files whose video is gone (after an upgrade or delete) to `config/orphans`. It does so only after two sightings a day apart, and never more than 20 at once.

## Quick start (Docker)

```bash
mkdir captionizerr && cd captionizerr
curl -LO https://raw.githubusercontent.com/captionizerr/captionizerr/main/deploy/docker-compose.yml
curl -L -o .env https://raw.githubusercontent.com/captionizerr/captionizerr/main/deploy/env.example
# edit .env: ANTHROPIC_API_KEY, CAPTIONIZERR_WEBHOOK_KEY, library path; edit the media volume in docker-compose.yml
docker compose up -d
```

Open `http://<host>:8095`, pick your target languages in **Settings**, and check **AI providers**.

Images: `ghcr.io/captionizerr/captionizerr` for linux/amd64 and linux/arm64 (Raspberry Pi 4/5 works fine; the model runs in the cloud or on your Ollama box).

### Connect Sonarr and Radarr

In each app: **Settings → Connect → + → Webhook**.

- URL: `http://<captionizerr>:8095/api/hooks/sonarr?apikey=<CAPTIONIZERR_WEBHOOK_KEY>` (use `radarr` for Radarr)
- Method: POST
- Triggers: **On Import**, **On Upgrade**, **On Rename**

Mount the library at the same path the *arr apps use, or set `CAPTIONIZERR_PATH_MAP`. For the Library page (translate existing episodes and films) and the Bazarr fallback, also set the Sonarr/Radarr URLs and keys.

> If Sonarr/Radarr **Import Extra Files** is on with `srt` in the list, Sonarr may adopt and rename Captionizerr's files. That's harmless, but leaving `srt` out keeps ownership clear.

### Local models (Ollama)

**AI providers → Local model (Ollama)**: enter the address, click **Find models**, pick one, and choose **Save and use**. Captionizerr uses Ollama's native API with an explicit context window and translates in chunks. Expect slower runs, and noticeably weaker results below ~27B parameters.

## Cost

With Anthropic, a typical 45–60 minute episode is about 16k input and 12k output tokens:

| Model | Per episode | Backlog (Batch, −50%) |
|---|---|---|
| Claude Opus 5.5 (default, low effort) | ~$0.31 | ~$0.16 |
| Claude Sonnet 5.5 | ~$0.15 | ~$0.08 |
| Claude Haiku 5.5 | ~$0.01 | ~$0.004 |
| Ollama (local) | $0 | — |

A film costs about twice an episode. Series notes cost $0.05–0.15 once per series. Opus at low effort was the quality reference in testing; Sonnet is a reasonable budget choice.

## Configuration

Everything except secrets can be changed in the web UI. Environment variables set the defaults; see [docs/configuration.md](docs/configuration.md) and [deploy/env.example](deploy/env.example).

**Security:** there is **no login by default**; it's meant for a home LAN. Set `CAPTIONIZERR_UI_PASSWORD` if anyone else can reach the port, and never expose it to the internet without a reverse proxy with auth. See [SECURITY.md](SECURITY.md).

## How it works

1. **Pick a source.** Embedded text track → sidecar → Bazarr (download, sync, wait). Image subtitles (PGS/VobSub) are not OCR'd.
2. **Send the whole episode in one request.** Stable rules and series notes are prompt-cached, and each line carries its screen time so the model can manage reading speed.
3. **Parse `N|text` lines.** Re-request skipped lines, validate, and run up to two fix rounds on flagged lines only.
4. **Write `<video>.captionizerr.<lang>.<fmt>`.** Then tell Jellyfin to rescan and send an ntfy spot-check.

## CLI

```bash
captionizerr probe "Show - S01E01.mkv"
captionizerr translate "Show - S01E01.mkv" --lang de --write
captionizerr translate episode.en.srt --lang ru --context notes.md --out episode.ru.srt --report run.json
captionizerr serve --port 8095

# quality check-set (private: cases contain copyrighted dialogue)
captionizerr eval-build --data /config --out /config/evals      # from translations you approved or edited
captionizerr eval /config/evals --provider ollama --model gemma3:12b
captionizerr backfill-sources      # once, after upgrading from a version before 0.1
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest          # needs ffmpeg on PATH for the media tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[MIT](LICENSE). Not affiliated with Sonarr, Radarr, Bazarr, Jellyfin, Anthropic or Ollama.
