# Configuration

Environment variables (prefix `CAPTIONIZERR_`) set the defaults. Values saved in the web UI override them and are stored in `config/captionizerr.db`. **Secrets (keys, passwords) can only be set in the environment** and are never shown in the UI.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Anthropic key (no prefix) |
| `WEBHOOK_KEY` | | Shared secret for `/api/hooks/*` and the JSON API (`?apikey=` or `X-Api-Key`) |
| `UI_PASSWORD` / `UI_USER` | empty / `admin` | HTTP Basic login for the web UI; empty = no login |
| `DATA_DIR` | `/config` | Database, notes, cache, history |
| `LIBRARY_ROOTS` | `/downloads` | `;`-separated; only videos under these are processed |
| `PATH_MAP` | | `arrprefix=ourprefix;…` when the *arr paths differ from ours |
| `LANGUAGES` | | Target languages (ISO 639-1), e.g. `de` or `ru,uk` |
| `SOURCE_LANGS` | `en` | Translate from these, in order |
| `ANY_SOURCE` | `1` | Otherwise use any other text subtitle |
| `PROVIDER` | `anthropic` | `anthropic`, `ollama` or `openai` |
| `MODEL` / `EFFORT` | `claude-opus-5-5` / `low` | Anthropic model and effort |
| `OLLAMA_URL` / `OLLAMA_MODEL` / `OLLAMA_NUM_CTX` | `http://localhost:11434` / / `16384` | Local models |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | | Any OpenAI-compatible API |
| `OPENAI_PRICE_IN` / `OPENAI_PRICE_OUT` / `OPENAI_MAX_OUTPUT` | `0` / `0` / `16000` | $/1M tokens for the budget; output limit for chunking |
| `MONTHLY_CAP_USD` / `ALERT_AT` | `20` / `0.8` | Budget cap and warning fraction |
| `TONE` | `faithful` | Swearing: `faithful`, `softened`, `family` |
| `SONGS` | `verse` | `verse`, `literal`, `original`, `skip` |
| `MAX_LINE` / `TARGET_CPS` | `42` / `20` | Readability limits (CJK targets use 16 / 9) |
| `AUTO_NOTES` | `1` | Draft series notes on a series' first translation |
| `SONARR_URL` / `SONARR_KEY`, `RADARR_URL` / `RADARR_KEY` | | Library page, Bazarr ID lookup |
| `BAZARR_URL` / `BAZARR_KEY` | | Fallback source download |
| `JELLYFIN_URL` / `JELLYFIN_KEY` / `JELLYFIN_PATH_MAP` | | Rescan after writing |
| `NTFY_URL` / `NTFY_TOPIC` / `PUBLIC_URL` | | Notifications and the link in them |

## Files in `/config`

| Path | What | Cleanup |
|---|---|---|
| `captionizerr.db` | jobs, spend ledger, settings, per-series overrides | unpaid failed or skipped jobs after 180 days |
| `context/<series>.md` | series notes | never (yours) |
| `cache/` | extracted source tracks | 60 days unused, 1 GB cap |
| `history/` | previous versions of edited files | newest 10 per file, 180 days |
| `feedback/` | editor corrections per series | last 1000 |
| `orphans/` | our files whose video disappeared | 30 days |

## Monitoring

`GET /metrics` (Prometheus): `captionizerr_jobs{state}`, `captionizerr_spend_month_usd`, `captionizerr_budget_usd`, `captionizerr_storage_bytes{kind}`, `captionizerr_maintenance_last_run_timestamp`. `GET /health` returns JSON.
