# docker-image-autobuild

A small, self-hosted service that watches GitHub repositories and, whenever a
branch gets a new commit, **pulls it, builds a Docker image, and pushes the
image** to your registry (GHCR, Docker Hub, or any other registry).

- **Change detection** by polling (`git ls-remote`, cheap, no clone needed) and/or
  **GitHub webhooks** for instant builds
- **Pull** with clone-once / fetch-after, pinned to the exact commit, submodules included
- **Build** with `docker build`, or `docker buildx` for multi-arch (`platforms = [...]`)
- **Push** every configured tag: `latest`, short SHA, branch, timestamp
- OCI labels (`source`, `revision`, `created`) on every image
- Persistent state, so restarts don't trigger rebuilds; failed builds retry with
  exponential backoff (max 1h)
- Private repos via token (sent as an HTTP header, never written to disk)
- Only the Python standard library, nothing to `pip install`

## Quick start (Docker Compose)

```bash
cp autobuild.example.toml autobuild.toml   # add your repos, images and registries
cp .env.example .env                       # add your tokens
docker compose up -d --build
docker compose logs -f
```

The container uses the host's Docker daemon through `/var/run/docker.sock`.

## Running it without Docker

You need Python 3.11+, `git`, and the `docker` CLI.

```bash
export GHCR_USER=me GHCR_TOKEN=ghp_... WEBHOOK_SECRET=...
python -m autobuild -c autobuild.toml run          # watch forever
python -m autobuild -c autobuild.toml once         # check everything once, build what changed
python -m autobuild -c autobuild.toml once --force my-app   # rebuild one repo now
python -m autobuild -c autobuild.toml status       # last build per repo
python -m autobuild -c autobuild.toml check-config
```

`once` works well from cron or a systemd timer if you'd rather not run a daemon.

## Configuration

See [`autobuild.example.toml`](autobuild.example.toml) for every option. A minimal setup:

```toml
[[registry]]
server = "ghcr.io"
username = "${GHCR_USER}"
password_env = "GHCR_TOKEN"

[[repo]]
name = "my-app"
url = "https://github.com/OWNER/my-app.git"
branch = "main"
image = "ghcr.io/owner/my-app"
tags = ["latest", "{sha}"]
```

Tag placeholders: `{sha}` (7 chars), `{full_sha}`, `{branch}`, `{timestamp}`.
Characters that aren't valid in tags are replaced, so `feature/x` becomes `feature-x`.

### Tokens

| Purpose | Token |
|---|---|
| Push to GHCR | PAT with `write:packages` (`password_env`) |
| Push to Docker Hub | Access token with Read & Write (`password_env`) |
| Pull a private repo | PAT / fine-grained token with Contents: read (`token_env`) |

## Webhooks (optional, for instant builds)

Set `webhook_port` (for example `8080`) and `webhook_secret_env`, then add a webhook in
GitHub under **Settings → Webhooks**:

- Payload URL: `http://your-host:8080/webhook`
- Content type: `application/json`
- Secret: the same value as `$WEBHOOK_SECRET`
- Events: *Just the push event*

A webhook only triggers a check. The service still asks the remote for the branch
head, so a forged payload can't make it build an arbitrary commit. Keep polling
turned on as a fallback; `poll_interval = 300` is plenty when webhooks are set up.

### HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook` | GitHub push events (HMAC-SHA256 verified when a secret is set) |
| `POST` | `/trigger/<repo>` | Forced rebuild; needs `Authorization: Bearer $WEBHOOK_SECRET` |
| `GET` | `/status` | JSON state for every repo |
| `GET` | `/healthz` | Liveness |

```bash
curl -X POST -H "Authorization: Bearer $WEBHOOK_SECRET" http://localhost:8080/trigger/my-app
```

## How it works

```
 scheduler (every poll_interval) ─┐
 webhook / manual trigger ────────┤──► queue (de-duplicated) ──► build worker (one build at a time)
                                  │
 build worker:  git ls-remote ─► changed? ─► clone/fetch + checkout SHA ─► docker build ─► docker push ─► save state
```

Builds run one at a time so several repos changing at once don't overload the host.

## Security notes

- Mounting `/var/run/docker.sock` gives the container root-equivalent access to the
  host. Only watch repositories you trust, because their Dockerfiles run on your daemon.
- If you expose the webhook port to the internet, set a webhook secret and put the
  port behind HTTPS (a reverse proxy, or a tunnel such as Cloudflare Tunnel).
- Secrets are redacted from logs and error messages.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The pipeline tests use a real local git repository and a fake `docker` binary, so
they don't need a Docker daemon.
