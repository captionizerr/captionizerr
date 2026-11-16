# Security

Captionizerr is built for a home network. The web UI has **no login unless you set `CAPTIONIZERR_UI_PASSWORD`**, and anyone who can open it can change settings and spend your API budget.

- Set `CAPTIONIZERR_UI_PASSWORD` if other people or devices you don't control can reach the port.
- Don't expose it to the internet directly. Put it behind a reverse proxy with authentication, or a VPN.
- State-changing UI requests must come from the UI's own pages (Origin/Referer check), which blocks cross-site requests from other tabs.
- Webhooks and the JSON API require `CAPTIONIZERR_WEBHOOK_KEY`. Prefer the `X-Api-Key` header where your client supports it, because a query-string key can end up in proxy logs.
- API keys live only in the environment and are never returned by the UI or API.
- The container needs write access to your media library, but only ever creates `*.captionizerr.<lang>.*` files there.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability") rather than a public issue.
