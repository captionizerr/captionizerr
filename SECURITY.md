# Security

Captionizerr is built for a home network. **Until you set a login** (setup wizard or **Settings → Security**, or `CAPTIONIZERR_UI_PASSWORD`), anyone who can open the web UI can change settings, spend your API budget, and make the server contact addresses they choose (the connection **Test** buttons).

## Recommendations

- **Set a login** if other people or devices you don't control can reach the port.
- **Don't expose it to the internet directly.** Put it behind a reverse proxy with HTTPS and authentication, or a VPN (see below).
- **Give the Anthropic key its own spend limit** in the Anthropic console, as a second cap beside Captionizerr's monthly budget.
- For locked-down deploys, provide secrets as **files** (`<NAME>_FILE`, compose secrets) rather than typing them in the UI.

## What Captionizerr does

- **Secrets:**
  - They come from a file (`<NAME>_FILE`), the environment, or the UI, in that order.
  - Keys typed in the UI go to `config/secrets.json` (mode 600), never into the database, and are never displayed again (write-only fields).
  - Secrets set by file or environment can't be changed from the UI.
  - The UI password is stored as a salted PBKDF2 hash.
  - Secrets never appear in logs (the HTTP access log is off, because URLs can carry the webhook key), in API responses or in the settings object's repr.
- **Login and keys:**
  - HTTP Basic login, compared in constant time.
  - **Lockout:** 10 failed logins or API keys from one address within 10 minutes lock it out for 15 minutes. This is in memory, and behind a reverse proxy the address is the proxy's, so everyone waits.
- **Webhooks and the JSON API** require the webhook key. Sonarr and Radarr send it as the HTTP Basic password, which **Set up webhook** configures. The `X-Api-Key` header also works. A `?apikey=` URL works too, but URLs end up in logs.
- **Cross-site requests:** state-changing UI requests must come from Captionizerr's own pages (Origin/Referer check), which blocks requests from other browser tabs.
- **Untrusted input:**
  - File paths from requests must be videos under `CAPTIONIZERR_LIBRARY_ROOTS`.
  - Series keys must look like generated slugs, because they become file names.
  - Redirect targets must be local paths.
  - Pages are HTML-escaped.
- **Writing to your library:** the container needs write access, but only ever creates or replaces its own `*.captionizerr.<lang>[.forced].srt|ass` files there. It never deletes; replaced files move to `config/orphans/`.
- **The image** runs as uid 1000, not root, and contains no secrets or local data.

## HTTPS with Caddy

Basic auth over plain HTTP is only encoded, not encrypted. If you set a UI password, put Captionizerr behind TLS. A minimal Caddyfile, with Caddy's internal CA for a LAN name (or a real domain for Let's Encrypt):

```
captionizerr.lan {
    tls internal
    reverse_proxy captionizerr:8095
}
```

Then point Sonarr/Radarr webhooks at `https://captionizerr.lan/api/hooks/…`, or keep them on the LAN address.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (Security tab → "Report a vulnerability") rather than a public issue.
