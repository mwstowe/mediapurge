# MediaPurge

<p align="center"><img src="MediaPurge.png" alt="MediaPurge" width="200"></p>

Automated media lifecycle management for Plex. Manages deletion and relocation of media across Plex, Sonarr, Radarr, Medusa, and Ombi based on configurable rules.

## What It Does

- Evaluates rules against your Plex libraries to determine what media should be cleaned up or moved
- Deletes media through the managing application (Sonarr, Radarr, or Medusa) so downloads don't recur
- Moves media between libraries and managers (e.g., Medusa → Sonarr, or between Radarr root folders)
- Cleans up associated Ombi requests when media is deleted
- Auto-approves Ombi requests for media already in Plex (matched by TVDB/IMDB/TMDB ID)
- Detects orphaned media (exists in Plex but isn't managed by any app)
- Confirms with users via email before acting, with configurable grace periods and keep methods
- Provides a web UI for browsing Plex libraries, managing rules, and running maintenance
- Reports space recovered after maintenance runs

## How Rules Work

### Scopes

Rules are evaluated from most specific to least specific. The first matching scope wins:

1. **Episode** — applies to a single episode
2. **Season** — applies to all episodes in a specific season
3. **Show / Movie** — applies to a specific show or movie
4. **Library** — applies to everything in a Plex library

Multiple rules can exist at the same scope (e.g., two library rules for "Movies"). Each is evaluated independently — first trigger that fires wins.

If no rule matches, the default action is **keep**.

### Actions

| Action | Description |
|--------|-------------|
| **Keep** | Protect this media from broader delete/move rules |
| **Delete** | Remove media when trigger conditions are met |
| **Move** | Relocate media to a different library/manager when conditions are met |

### Triggers

Each rule has one or more triggers that determine *when* the action fires:

| Trigger Type | Fires when... |
|-------------|---------------|
| **After watched** | Specified user(s) have watched the media, and a grace period has passed |
| **After inactivity** | No one has watched anything in the show/movie for N days |
| **After age** | N days have passed since the media was added to Plex |

Each trigger independently specifies whether to act immediately or confirm first.

### Episode Handling (shows only, delete action)

| Option | Description |
|--------|-------------|
| **Processing mode** | Per episode (delete individual episodes) or per season (delete whole seasons when all episodes qualify) |
| **Min episodes/seasons** | Always keep the latest N, even if they qualify |
| **Protect On Deck** | Skip items on a user's on-deck list |
| **Remove show when empty** | After all episodes are deleted: never / if ended / always remove the show from its manager |

Move rules always operate on the entire show — no per-episode moves.

### Confirmation Workflow

When a trigger is set to "Confirm first":

1. Media becomes eligible per the trigger condition
2. An email is sent to the configured user(s) with available keep methods
3. The system waits N days for a response
4. **If the user acts** → deletion/move is cancelled, a "kept" confirmation email is sent
5. **If no response** → action proceeds

Available keep methods (depends on trigger type):

| Trigger Type | Valid keep methods |
|-------------|-------------------|
| After watched | Mark as unwatched, Snooze (reset timer), Disable trigger |
| After inactivity | Start watching, Snooze, Disable trigger |
| After age | Snooze, Disable trigger |

Confirmation emails include clickable links and an explicit deletion date.

### Move Feature

Moves relocate media between libraries or managers:

- **Radarr → Radarr**: Uses Radarr's native move API (atomic)
- **Sonarr → Sonarr**: Uses Sonarr's native move API (atomic)
- **Sonarr ↔ Medusa**: Moves files, adds to new manager, removes from old manager with rollback on failure
- **Episode status is preserved**: Ignored/Skipped → Unmonitored (and vice versa)

Safety features:
- Pre-flight checks (disk space, permissions, destination manager reachability)
- Source show is unmonitored before file moves to prevent redownloads
- Rollback if the destination manager rejects the show
- Same-filesystem moves are instant (rename, not copy)

## Configuration

Copy `config.yaml.example` to `config.yaml` and fill in your values.

### Connections

```yaml
plex:
  url: http://127.0.0.1:32400
  token: "YOUR_PLEX_TOKEN"
  user_tokens:
    alice: "ALICE_PLEX_TOKEN"
    bob: "BOB_PLEX_TOKEN"
  user_emails:
    admin: admin@example.com
    alice: alice@example.com

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

#### User Tokens

`user_tokens` provides per-user Plex tokens so MediaPurge can check watch status for individual users. Without a token for a user, it falls back to the admin token — which gives incorrect results.

The admin/server owner doesn't need an entry — the main `token` is used for them.

To get a user's token:
```bash
python3 -c "
from plexapi.server import PlexServer
s = PlexServer('http://127.0.0.1:32400', 'YOUR_ADMIN_TOKEN')
for u in s.myPlexAccount().users():
    print(f'{u.username or u.title}: {u.get_token(s.machineIdentifier)}')
"
```

### Web UI

```yaml
web:
  secret_key: "RANDOM_SECRET"
  admin_password: "$2b$12$..."  # bcrypt hash
  base_url: "https://mediapurge.example.com:9393"
  port: 9393
  ssl_cert: /opt/mediapurge/ssl/fullchain.pem
  ssl_key: /opt/mediapurge/ssl/privkey.pem
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
    from: mediapurge@example.com
    admin: admin@example.com
    smtp_host: smtp.example.com
    smtp_port: 587
    smtp_user: ""
    smtp_pass: ""
  discord:
    webhook_url: ""
```

### Maintenance

```yaml
maintenance:
  dry_run: false
  schedule: "03:00"
  log_file: /var/log/mediapurge.log
  excluded_libraries:
    - "3D Movies"
```

## Installation

### Dependencies

Gentoo:
```bash
sudo emerge -av flask sqlalchemy pyyaml requests bcrypt PlexAPI
```

Other systems (pip):
```bash
pip install flask sqlalchemy pyyaml requests bcrypt plexapi
```

### Deploy

```bash
sudo mkdir -p /opt/mediapurge
sudo git clone https://github.com/mwstowe/mediapurge.git /opt/mediapurge
sudo chown -R sabnzbd:sabnzbd /opt/mediapurge
cp /opt/mediapurge/config.yaml.example /opt/mediapurge/config.yaml
# Edit config.yaml with your credentials
```

To update:
```bash
cd /opt/mediapurge && sudo -u sabnzbd git pull
sudo systemctl restart mediapurge
```

Create the systemd service at `/etc/systemd/system/mediapurge.service`:
```ini
[Unit]
Description=MediaPurge Web UI
After=network.target plex-media-server.service

[Service]
Type=simple
User=sabnzbd
Group=sabnzbd
WorkingDirectory=/opt/mediapurge
ExecStart=/usr/bin/python3.13 -m mediapurge.web.app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Running

The systemd service runs both the web UI and the scheduled maintenance:

```bash
sudo systemctl enable --now mediapurge
```

Access the web UI at `https://your-host:9393`.

### Manual Maintenance

From the web UI: **Maintenance** → **Preview (Dry Run)** or **Run Now (Live)**.

From the command line:
```bash
sudo -u sabnzbd python3.13 -m mediapurge.maintenance --dry-run
sudo -u sabnzbd python3.13 -m mediapurge.maintenance
```

## Web UI Pages

| Page | Purpose |
|------|---------|
| Dashboard | Summary stats, recent actions |
| Browse | Navigate Plex libraries with thumbnails, watch status, manager info, existing rules |
| Rules | List/create/edit/delete rules with triggers |
| Orphans | Async scan for media in Plex not managed by any app |
| Maintenance | Dry-run preview and live execution with space reporting |
| Log | Action history |
| Config | Edit configuration, test connections, test email, change password (passwords masked) |
