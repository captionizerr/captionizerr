# Configuration

Everything can be set in the web UI (the setup wizard and **Settings**). The environment variables below are **optional**, for automated or locked-down deploys.

**Settings** (languages, model, URLs and so on):
- Environment values are the defaults.
- Values saved in the UI override them and are stored in `config/captionizerr.db`.
- **Settings → System → Changed here** lists the overrides, with a reset button.

**Secrets** (API keys, passwords) come from the first of:
1. **`<NAME>_FILE`**: a file such as a compose secret under `/run/secrets/` (e.g. `ANTHROPIC_API_KEY_FILE`, `CAPTIONIZERR_WEBHOOK_KEY_FILE`). This keeps the secret out of the container environment and `docker inspect`.
2. **`<NAME>`** in the environment.
3. **The UI**: stored in `config/secrets.json` (mode 600, never in the database).

Secrets from 1 or 2 show as "Set on the server" and can't be changed in the UI. No secret is ever displayed. The one exception is **Show key** for a webhook key the UI generated, because you may need to paste it into Sonarr or Radarr by hand. A webhook key is created automatically on first run if none is given, and a UI password is stored as a salted PBKDF2 hash.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Anthropic key (no prefix) |
| `WEBHOOK_KEY` | generated on first run | Shared secret for `/api/hooks/*` and the JSON API: HTTP Basic password (any user), `X-Api-Key` header, or `?apikey=` |
| `UI_PASSWORD` / `UI_USER` | empty / `admin` | HTTP Basic login for the web UI (or set it in Settings → Security); empty = no login |
| `DATA_DIR` | `/config` | Database, notes, cache, history |
| `LIBRARY_ROOTS` | `/downloads` | `;`-separated; only videos under these are processed |
| `PATH_MAP` | | `arrprefix=ourprefix;…` when the *arr paths differ from ours |
| `LANGUAGES` | | Target languages (ISO 639-1), e.g. `de`, or `es,fr` |
| `SOURCE_LANGS` | `en` | Translate from these, in order |
| `ANY_SOURCE` | `1` | Otherwise use any other text subtitle |
| `PROVIDER` | `anthropic` | `anthropic`, `ollama` or `openai` |
| `MODEL` / `EFFORT` | `claude-opus-5-5` / `low` | Anthropic model and effort |
| `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_NUM_CTX` | `http://localhost:11434` / / `16384` | Local models |
| `OLLAMA_EFFORT` | `off` | Thinking for thinking models: `off`, `low`, `medium`, `high` (falls back to on/off, or none, where a model has no levels) |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | | Any OpenAI-compatible API |
| `OPENAI_EFFORT` | *(empty)* | `reasoning_effort` for reasoning models: empty = model default, or `minimal`, `low`, `medium`, `high`. Dropped, with a note in the job, if the model rejects it |
| `OPENAI_PRICE_IN` / `OPENAI_PRICE_OUT` / `OPENAI_MAX_OUTPUT` | `0` / `0` / `16000` | $/1M tokens for the budget (filled in automatically when you pick a model in the UI: OpenRouter live prices or the LiteLLM community list; $0 for local servers); output limit for chunking |
| `MONTHLY_CAP_USD` / `ALERT_AT` | `20` / `0.8` | Budget cap and warning fraction |
| `TONE` | `faithful` | Swearing: `faithful`, `softened`, `family` |
| `SONGS` | `verse` | `verse`, `literal`, `original`, `skip` |
| `MAX_LINE` / `TARGET_CPS` | `42` / `20` | Readability limits (CJK targets use 16 / 9) |
| `AUTO_NOTES` | `1` | Draft series notes on a series' first translation |
| `SONARR_URL` / `SONARR_KEY`, `RADARR_URL` / `RADARR_KEY` | | Library page, Bazarr ID lookup |
| `BAZARR_URL` / `BAZARR_KEY` | | Fallback source download |
| `WHISPER_URL` | | Speech-to-text service (whisper-asr-webservice API, e.g. `deploy/whisper`): downloaded subtitles and endings taken from another release are moved onto the speech, and jobs get **Re-sync to speech** |
| `SPEECH_SYNC` | `auto` | When Whisper is used automatically: `auto` = downloaded subtitles and endings taken from another file; `all` = every translation, built-in tracks too (a few minutes of the Whisper machine per episode); `off` = only the **Re-sync to speech** button. Also in Settings → Connections |
| `JELLYFIN_URL` / `JELLYFIN_KEY` / `JELLYFIN_PATH_MAP` | | Rescan after writing |
| `PLEX_URL` / `PLEX_TOKEN` / `PLEX_PATH_MAP` | | Plex folder rescan after writing (Emby: use the Jellyfin settings) |
| `OPENSUBTITLES_USER` / `OPENSUBTITLES_PASSWORD` | | Your OpenSubtitles.org account, for the opt-in **Share…** button (editor and job pages) |
| `OPENSUBTITLES_USER_AGENT` | `Captionizerr v<version>` | Client name sent to OpenSubtitles; set it if they assign you one |
| `NTFY_URL` / `NTFY_TOPIC` / `PUBLIC_URL` | | Notifications and the link in them |

## Files in `/config`

| Path | What | Cleanup |
|---|---|---|
| `captionizerr.db` | jobs, spend ledger, settings, per-series overrides | unpaid failed or skipped jobs after 180 days |
| `context/<series>.md` | series notes | never automatically; Settings → System → *Notes for removed series* deletes those of series no longer in Sonarr/Radarr |
| `cache/` | extracted source tracks | 60 days unused, 1 GB cap |
| `history/` | previous versions of edited files | newest 10 per file, 180 days |
| `feedback/` | editor corrections per series | last 1000; removed with the notes above |
| `orphans/` | our files whose video disappeared, and translations replaced after an upgrade | 30 days |
| `sources/` | the source each translation was made from (re-timing, editor, evals) | 30 days after the translation is gone |
| `secrets.json` | keys and passwords entered in the UI (mode 600; the UI password as a PBKDF2 hash) | never |
| `backup/` | daily consistent database snapshot (newest 7): **back up this folder, not the live `captionizerr.db`** | 7 kept |
| `evals/` | your quality check cases (private) | never |

Back up everything in `/config` except `cache/`, `tmp/` and the live `captionizerr.db*` files. The UI shows which settings were changed there and override the environment, with a reset button.

## Monitoring

`GET /metrics` (Prometheus): `captionizerr_jobs{state}`, `captionizerr_spend_month_usd`, `captionizerr_budget_usd`, `captionizerr_storage_bytes{kind}`, `captionizerr_maintenance_last_run_timestamp`. `GET /health` returns JSON.

## Sharing on OpenSubtitles

The editor and each job page have a **Share…** button. The confirmation page:
- identifies the title (OpenSubtitles' own guess from the filename; check the IMDb ID, and for an episode it must be the episode's);
- shows the language, flags, release name and credit;
- requires two ticks: you reviewed the file, and you accept OpenSubtitles' terms.

Uploads use OpenSubtitles' XML-RPC upload API (the one their own uploader uses):
- **Always marked as an AI translation** (`automatictranslation=1`).
- **Forced files** are marked "foreign parts only".
- **Duplicates** (the same file) are detected before uploading.
- **Each share is recorded** and shown on the page with its link.

Check OpenSubtitles' current rules for AI-translated uploads before sharing. Translated subtitles are derivative works of the dialogue: you are responsible for what you upload.
