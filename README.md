# MediaCleaner

Automated media lifecycle management for Plex. Manages deletion of watched media across Plex, Sonarr, Radarr, Medusa, and Ombi based on configurable rules.

## What It Does

- Evaluates rules against your Plex libraries to determine what media should be cleaned up
- Deletes media through the managing application (Sonarr, Radarr, or Medusa) so downloads don't recur
- Cleans up associated Ombi requests when media is deleted
- Detects orphaned media (exists in Plex but isn't managed by any app)
- Optionally confirms with users via email before deleting, with a configurable grace period
- Provides a web UI for browsing Plex libraries, managing rules, and running maintenance

## How Rules Work

### Scopes

Rules are evaluated from most specific to least specific. The first matching rule wins:

1. **Episode** — applies to a single episode
2. **Season** — applies to all episodes in a specific season
3. **Show / Movie** — applies to a specific show or movie
4. **Library** — applies to everything in a Plex library (e.g., "TV Shows", "Movies")

If no rule matches, the default action is **keep** (never delete without an explicit rule).

### Rule Fields

| Field | Description |
|-------|-------------|
| **Action** | `keep` or `delete`. Keep rules exist to override broader delete rules. |
| **Watched By** | Which user(s) must have watched the media. Can select multiple users or "any." |
| **Min Days Watched** | Grace period — days after an item is watched before it's eligible for deletion. |
| **Max Days Age** | Hard age limit — delete after this many days since added, regardless of watch status. |
| **Max Days Inactive** | Delete entire show if no episodes have been watched in this many days. |
| **Min Episodes** | Always keep the latest N episodes, even if they qualify for deletion. |
| **Protect On Deck** | Skip items currently on a user's on-deck list. |
| **All Watched** | (Shows only) Wait until every episode is watched before taking action on the show as a whole. |
| **Confirm Before Delete** | Send an email and wait before deleting. User can cancel. Defaults to on. |
| **Confirm Days** | How long to wait for user response. No response = proceed with deletion. |
| **Confirm Method** | How the user keeps the media: click a URL, start watching, or mark as unwatched. |

### Episode Processing Order

For show-scoped rules that affect individual episodes:

- Episodes are processed **oldest first** (S01E01, S01E02, ...)
- Processing **stops at the first episode that doesn't qualify** — no skipping
- The latest `min_episodes` are always protected
- If all episodes qualify AND the show has ended, it collapses to a whole-show deletion

### Confirmation Workflow

When `confirm_before_delete` is enabled:

1. Media becomes eligible for deletion per the rule
2. An email is sent to the configured user with instructions
3. The system waits `confirm_days` for a response
4. If the user clicks the keep URL / starts watching / marks unwatched → deletion is cancelled and the rule is snoozed for the full timer period
5. If no response → media is deleted

### Rule Auto-Cleanup

- Rules that delete an entire show or movie are automatically removed after the deletion completes
- Rules that manage individual episodes persist as long as the show exists

## Configuration

Copy `config.yaml.example` to `config.yaml` and fill in your values.

### Connections

```yaml
plex:
  url: http://127.0.0.1:32400
  token: "YOUR_PLEX_TOKEN"
  user_emails:
    mstowe: mstowe@example.com
    epstowe: evelyn@example.com

sonarr:
  url: http://127.0.0.1:8989
  api_key: "YOUR_KEY"

radarr:
  url: http://127.0.0.1:7878
  api_key: "YOUR_KEY"

medusa:
  url: https://127.0.0.1:8081
  api_key: "YOUR_KEY"

ombi:
  url: http://127.0.0.1:5000
  api_key: "YOUR_KEY"
```

`user_emails` maps Plex usernames to email addresses for confirmation emails. Entries that aren't Plex users are still available as email destinations.

### Web UI

```yaml
web:
  secret_key: "RANDOM_SECRET"
  admin_password: "$2b$12$..."  # bcrypt hash
  base_url: "https://mediacleaner.example.com:9393"
  port: 9393
  ssl_cert: /opt/mediacleaner/ssl/fullchain.pem
  ssl_key: /opt/mediacleaner/ssl/privkey.pem
```

Generate a password hash:
```bash
python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
```

### Notifications

```yaml
notifications:
  enabled: true
  method: email
  email:
    from: mediacleaner@example.com
    admin: admin@example.com
    smtp_host: smtp.example.com
    smtp_port: 587
    smtp_user: ""
    smtp_pass: ""
  discord:
    webhook_url: ""
```

`admin` receives maintenance reports and test emails. `from` is the sender address.

### Maintenance

```yaml
maintenance:
  dry_run: false
  schedule: "03:00"
  log_file: /var/log/mediacleaner.log
  excluded_libraries:
    - "3D Movies"
```

- `dry_run`: When true, maintenance evaluates but never deletes.
- `schedule`: Daily run time (24h format). Runs inside the web service.
- `excluded_libraries`: Libraries to skip entirely during evaluation and orphan scanning.

## Installation

Requirements (Gentoo):
```bash
sudo emerge -av flask sqlalchemy pyyaml requests bcrypt PlexAPI
```

Deploy:
```bash
./deploy.sh
```

This rsyncs the code to `/opt/mediacleaner` (owned by `sabnzbd:sabnzbd`), preserving `config.yaml` and the database.

## Running

The systemd service runs both the web UI and the scheduled maintenance:

```bash
sudo systemctl enable --now mediacleaner
```

Access the web UI at `https://your-host:9393`.

### Manual Maintenance

From the web UI: **Maintenance** → **Preview (Dry Run)** or **Run Now (Live)**.

From the command line:
```bash
sudo -u sabnzbd python3.13 -m mediacleaner.maintenance --dry-run
sudo -u sabnzbd python3.13 -m mediacleaner.maintenance
```

## Web UI Pages

| Page | Purpose |
|------|---------|
| Dashboard | Summary stats, recent actions |
| Rules | List/create/edit/delete rules |
| Browse | Navigate Plex libraries with thumbnails, watch status, manager info |
| Orphans | Media in Plex not managed by any app |
| Maintenance | Dry-run preview and live execution |
| Log | Action history |
| Config | Edit configuration, test connections, test email, change password |
