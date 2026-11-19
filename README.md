# ⚡ Quick

This is a small, self-hosted version of [Shopify's internal Quick platform](https://shopify.engineering/quick). You put a folder of HTML on it and get a live site, and every site comes with a backend with nothing to set up.

```
python quick.py serve                       # start the server
python quick.py init my-site                # starter page + CLAUDE.md for coding agents
python quick.py deploy my-site              # → http://my-site.localhost:8000/
```

`http://localhost:8000/` shows a dashboard of every site. On Windows you can type `quick ...` instead of `python quick.py ...`, because `quick.cmd` is in this folder.

## What every site gets

Add `<script src="/_quick/quick.js"></script>` to a page and it gets these:

| API | Example |
|---|---|
| Database (schemaless, realtime) | `quick.db.collection("todos").create({...})`, `.watch(docs => ...)` |
| Rooms (pub/sub + presence) | `quick.room("lobby").on("message", fn).send({x, y})` |
| AI (Claude, key stays on server) | `await quick.ai.ask("...")`, `quick.ai.stream(...)`, `quick.ai.json(prompt, schema)`, images/PDFs via `quick.ai.image(file)` / `quick.ai.pdf(file)` |
| Files | `await quick.files.upload(file)` → `{url}` |
| Identity | `await quick.me()` → `{name}` |

[AGENT_GUIDE.md](AGENT_GUIDE.md) has the full API. `quick init` copies it into each new site as `CLAUDE.md`, so a coding agent can build on Quick without being told how.

## Apps

- **[Notes Lab](apps/notes/)**: photograph handwritten notes and Claude transcribes them, rendering the math properly. From those notes it can write summaries, give quizzes with graded feedback, generate practice problems with hidden solutions, and make flashcards with spaced repetition. It's one HTML file.
- **[Lunch vote](examples/lunch-vote/)**: a small demo of realtime voting with presence and AI suggestions.

## How it maps to Shopify's design

| Shopify | Here |
|---|---|
| GCS bucket, one folder per site | `data/sites/<name>/` |
| NGINX wildcard subdomain → folder | Python server reads the `Host` header |
| `quick deploy` = gcloud rsync | `quick deploy` zips and uploads; the swap is atomic |
| Google IAP (only employees get in) | Off by default (local only); set `QUICK_SECRET` for a shared password |
| Cloud SQL document store | SQLite (`data/quick.db`) |
| WebSockets | Server-Sent Events (stdlib-only) |
| AI proxy | `/_quick/api/ai` → Claude via the Anthropic SDK |
| No owners, anyone can overwrite | Same, on purpose |

Shopify's BigQuery integration was left out.

## Config

| Env var | Default | |
|---|---|---|
| `QUICK_PORT` | `8000` | |
| `QUICK_HOST` | `127.0.0.1` | set `0.0.0.0` to share on your network or a server |
| `QUICK_DOMAIN` | `localhost` | e.g. `quick.example.com`, which needs wildcard DNS `*.quick.example.com` |
| `QUICK_SECRET` | unset | when set, viewers log in with a name + this password, and the CLI must send it too |
| `QUICK_USER` | your OS user | identity used when there is no login |
| `QUICK_PUBLIC_SITES` | unset | comma-separated sites that open without login; guests get the database and rooms, not AI or uploads |
| `QUICK_AI_MODEL` | `claude-opus-5-5` | |
| `QUICK_DATA` | `./data` | |

To turn on AI: run `pip install anthropic`, set `ANTHROPIC_API_KEY`, then restart the server.

## Live setup

The live version runs on one Ubuntu VPS behind Cloudflare, with Google sign-in:

```
Browser ── HTTPS ──▶ Cloudflare (*.domain, proxied) ── HTTPS, origin cert ──▶ VPS :443 (python server.py)
                                                       firewall: Cloudflare IPs only
```

- `deploy/setup.sh` does the one-time server setup: Python, a `quick` system user, the systemd service, the firewall and nightly backups to `/var/backups/quick`.
- To ship code changes, run `deploy/push.sh root@SERVER_IP`.
- Secrets live in `deploy/secrets/` (gitignored) and get copied to `/etc/quick/` on the server. Start from [`deploy/quick.env.example`](deploy/quick.env.example).
- Google sign-in is configured with `QUICK_GOOGLE_CLIENT_ID`, `QUICK_GOOGLE_CLIENT_SECRET` and `QUICK_ALLOWED_EMAILS` / `QUICK_ALLOWED_DOMAINS`. HTTPS is served directly with `QUICK_TLS_CERT` / `QUICK_TLS_KEY`, using a Cloudflare origin certificate.
- To deploy sites from your machine, set `QUICK_SERVER=https://yourdomain` and `QUICK_SECRET=<from quick.env>`, then run `python quick.py deploy my-site`.
- To view logs, run `ssh -i ~/.ssh/quick_ed25519 root@SERVER_IP journalctl -u quick -f`.

Python 3.9+ standard library only, apart from the optional `anthropic` package.
