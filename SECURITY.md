# Security

Captionizerr is built for a home network. The web UI has **no login unless you set `CAPTIONIZERR_UI_PASSWORD`**, and anyone who can open it can change settings and spend your API budget.

- Set `CAPTIONIZERR_UI_PASSWORD` if other people or devices you don't control can reach the port.
- Don't expose it to the internet directly. Put it behind a reverse proxy with authentication, or a VPN.
- State-changing UI requests must come from the UI's own pages (Origin/Referer check), which blocks cross-site requests from other tabs.
- Webhooks and the JSON API require `CAPTIONIZERR_WEBHOOK_KEY`: put it in the Sonarr/Radarr webhook's **Password** field (HTTP Basic) or an `X-Api-Key` header. A `?apikey=` URL still works but ends up in logs.
- **Secrets as files:** every secret accepts `<NAME>_FILE` (compose/Docker secrets), so keys stay out of the container environment and `docker inspect`. Secrets never go into the database, the UI, the API or the logs (the HTTP access log is off because URLs can carry the key), and `Settings` hides them from its repr.
- **Lockout:** 10 failed logins or API keys from one address within 10 minutes lock that address out for 15 minutes (in memory; behind a reverse proxy that is the proxy's address, so everyone waits).
- Give the Anthropic key its own spend limit in the Anthropic console as a second cap beside Captionizerr's monthly budget.
- The container needs write access to your media library, but only ever creates `*.captionizerr.<lang>.*` files there.

## HTTPS with Caddy

Basic auth over plain HTTP is only encoded, not encrypted. If you set a UI password, put Captionizerr behind TLS. A minimal Caddyfile, with Caddy's internal CA for a LAN name (or a real domain for Let's Encrypt):

```
captionizerr.lan {
    tls internal
    reverse_proxy 192.168.1.10:8095
}
```

Then point Sonarr/Radarr webhooks at `https://captionizerr.lan/api/hooks/…`, or keep them on the LAN address.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability") rather than a public issue.
