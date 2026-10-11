# GhorBox | غر‌باکس 📦

**Grumble. Suggest. Make the team better.**

A private Telegram feedback box for teams, with anonymous initial feedback,
two-way conversations, and separate internal and external feedback domains.
Suggested repository name: **`ghorbox`**.

[فارسی](README.md) · [Architecture and security (Persian)](docs/ARCHITECTURE.md)

## What it does — in 10 lines

1. Only members registered and enabled by an administrator can use the bot.
2. Administrators add up to ten members at once through Telegram's user picker.
3. Members select a colleague and write a criticism or suggestion.
4. After the author writes the text, it is submitted directly without another confirmation.
5. Recipients get the anonymous text as a direct Telegram bot notification.
6. Multiple conversations stay open; participants select one and chat anonymously without Reply.
7. There is no report or manual-close action; inactive conversations expire automatically.
8. External feedback has its own directory and administrators who mediate replies.
9. Main admins see all initial feedback; internal admins see only their group's, never its author.
10. Weekly privacy-filtered reports, encrypted backups and optional retention are included.

## Features

- Persian Telegram interface, bulk member selection, direct delivery and personal statistics.
- Internal, external and full administrator roles; separate permission to manage administrators.
- Three SQLite databases: identity, internal feedback and external feedback.
- Encrypted content and routing, service-level authorization and a durable delivery queue.
- Weekly reports with small-group suppression and bucketed counts.
- Encrypted backup, verification, restore into new directories and retention preview.
- Multi-stage Dockerfile, Docker Compose, systemd unit and offline automated tests.

## Local installation

Requirements: **Python 3.13** and **uv**. From the repository root, for a fresh installation:

```bash
git clone https://github.com/alirah75/ghorbox.git
cd ghorbox
uv sync --locked
uv run --locked python -m app.cli init-env
```

Edit `.env` locally. Set the BotFather token and initial administrators' numeric Telegram IDs.
Additional members can be added through the Telegram user picker after initialization.

| Variable | Purpose / default |
|---|---|
| `BOT_TOKEN` | Private bot token; required |
| `BOOTSTRAP_ADMIN_IDS` | Initial administrator IDs as a JSON array; required |
| `APP_TIMEZONE` | `Asia/Tehran` |
| `DATA_DIR` | `data` |
| `SECRETS_DIR` | `.secrets`, separate from data |
| `RETENTION_ENABLED` | `false`; automatic retention is opt-in |

```bash
uv run --locked python -m app.cli check-config
uv run --locked python -m app.cli keys-init
uv run --locked python -m app.cli db-upgrade
uv run --locked python -m app.cli db-check
uv run --locked python -m app.cli admin-bootstrap
uv run --locked python -m app.main
```

Do not repeat `init-env` or `keys-init` on an existing installation. If application records
were cleared but the schema and keys remain, run `db-check` and start the bot; startup
bootstraps the initial administrator again. Never replace lost keys with new ones to recover
encrypted data. Members and recipients must start a private chat with the bot.

## Docker Compose

Use [Dockerfile](Dockerfile) and [compose.yaml](compose.yaml). For a fresh installation on
Linux with Docker Engine and Compose v2, from the repository root:

```bash
sudo install -d -m 0700 -o 10001 -g 10001 deploy/runtime
sudo install -d -m 0700 -o 10001 -g 10001 deploy/runtime/data deploy/runtime/keys deploy/runtime/backups deploy/runtime/recovery
sudo install -m 0600 -o 10001 -g 10001 .env.example deploy/runtime/bot.env
sudo nano deploy/runtime/bot.env
docker compose build --pull
docker compose config --quiet
docker compose -f compose.yaml -f deploy/compose.setup.yaml run --rm bot python -m app.cli keys-init
docker compose run --rm bot python -m app.cli db-upgrade
docker compose run --rm bot python -m app.cli db-check
docker compose run --rm bot python -m app.cli admin-bootstrap
docker compose up -d
```

Enter the token and administrator IDs in `bot.env` first. Do not overwrite existing settings
or keys. The setup override temporarily makes the keys directory writable; never use it
to start the long-running bot. Normal execution uses UID/GID 10001, a read-only root
filesystem and key mount, and no published ports. Outbound HTTPS and DNS are required.
Keep exactly one active instance per token, with SQLite on local rather than network storage.

Data, keys, backups and restored datasets persist in `deploy/runtime` on the host.
For Docker Desktop/rootless Docker, verify filesystem permissions and UID mapping.
The [deployment guide (Persian)](docs/DEPLOYMENT.md) covers systemd, migration and rollback.

Offline image test, using temporary fake data with no Telegram requests:

```bash
docker run --rm --read-only --cap-drop=ALL --security-opt=no-new-privileges --network=none --tmpfs /tmp:rw,noexec,nosuid,size=128m,mode=1777 --mount type=bind,source="$PWD/deploy/smoke.py",target=/smoke.py,readonly ghorbox:1.0.0 python /smoke.py
```

## Tests and maintenance

The Docker image build and offline installation, database, backup and restore checks
passed on 2026-10-06. Live Telegram operation in the container still needs target-host acceptance.

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

Stop the bot before offline maintenance. Choose a new archive and restore-directory name each time:

```bash
uv run --locked python -m app.cli backup --output backups/first.afb
uv run --locked python -m app.cli backup-check --source backups/first.afb
uv run --locked python -m app.cli restore --source backups/first.afb --target recovery/first
uv run --locked python -m app.cli maintenance
```

Restore never overwrites an existing directory or switches the active data path.
`maintenance` only previews changes unless `--apply` is provided. Keep the original
keyring separately from backups; without it, encrypted data cannot be restored.
Automatic retention is disabled by default. See the [maintenance guide (Persian)](docs/TEST_STAGE_09.md).

## Privacy boundaries

An authorized administrator may see “an anonymous person sent this feedback to Ali,”
including the initial text and recipient, but not the author's name or Telegram ID.
This does not protect against full server/key access, Telegram itself, or identifying clues
in content or timing. The initial author chose the recipient, so a reply may be attributable.

Exactly-once Telegram delivery is not guaranteed. Ambiguous sends are not automatically
retried. Automated backup scheduling, automatic key rotation and multi-instance deployment
are not provided. Complete live acceptance and restore checks on the target host before
collecting real feedback; automated tests do not replace deployment or independent security review.

## Publishing

Suggested repository name: **`ghorbox`**. `.gitignore` excludes settings, keys, databases,
backups, logs and runtime directories. `.dockerignore` allows only build inputs. Publish
only the empty `.env.example`, never actual credentials or user data. Review
`git status --short` and `git diff --cached` before committing; never force-add private files.
Configure a remote after creating your own destination repository.

The bot interface and detailed operational guides are currently in Persian.
