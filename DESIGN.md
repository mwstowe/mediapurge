# MediaCleaner — Design Document

## Overview

MediaCleaner is a media lifecycle management system that replaces the current PlexCleaner JSON configs and shell scripts with a unified, database-backed rule engine and web UI.

It manages media deletion across Plex, Sonarr, Radarr, Medusa, and Ombi based on configurable rules tied to watch status, age, and episode retention limits.

## Goals

1. Single source of truth for all media lifecycle rules.
2. Rules settable at **category level** (Plex library section) and overridable at **individual media level**.
3. Deletion performed through the managing application (Sonarr/Radarr/Medusa), then confirmed via Plex.
4. Detection of **orphaned media** (exists on disk/Plex but unmanaged by any arr).
5. Web UI for rule management with single-user admin auth.
6. Daily maintenance job with dry-run mode and notifications.

## Architecture

```
mediacleaner/
├── mediacleaner/
│   ├── __init__.py
│   ├── config.py           # Load config.yaml
│   ├── db.py               # Database engine/session setup
│   ├── models.py           # SQLAlchemy ORM models
│   ├── clients/
│   │   ├── __init__.py
│   │   ├── plex.py         # Plex API client
│   │   ├── sonarr.py       # Sonarr API client
│   │   ├── radarr.py       # Radarr API client
│   │   ├── medusa.py       # Medusa API client
│   │   └── ombi.py         # Ombi API client
│   ├── engine.py           # Rule evaluation + orphan detection
│   ├── maintenance.py      # Daily job orchestration
│   ├── notify.py           # Notification dispatch (email/Discord)
│   └── web/
│       ├── __init__.py
│       ├── app.py          # Flask app factory
│       ├── auth.py         # Simple password auth
│       ├── routes.py       # Web routes
│       └── templates/      # Jinja2 templates
├── run_maintenance.py      # CLI entry point for cron
├── run_web.py              # CLI entry point for web server
├── config.yaml             # User configuration (connections, credentials)
├── pyproject.toml          # Project metadata + dependencies
└── mediacleaner.db         # SQLite database (created at runtime)
```

## Technology Choices

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Language | Python 3.11+ | Best Plex library ecosystem, matches existing tooling |
| Web framework | Flask | Lightweight, server-rendered templates, simple auth |
| Database | SQLite | Single-user, low write volume, zero ops overhead |
| ORM | SQLAlchemy 2.x | Standard, well-documented |
| Plex client | python-plexapi | Mature, full-featured |
| Arr clients | requests (direct) | Sonarr/Radarr/Medusa APIs are simple REST |
| Templates | Jinja2 + HTMX | Interactive UI without a JS build step |
| Scheduler | systemd timer or cron | External to the app, simplest approach |

## Configuration (config.yaml)

```yaml
plex:
  url: http://127.0.0.1:32400
  token: "YOUR_PLEX_TOKEN"

sonarr:
  url: http://127.0.0.1:8989
  api_key: "YOUR_SONARR_KEY"

radarr:
  url: http://127.0.0.1:7878
  api_key: "YOUR_RADARR_KEY"

medusa:
  url: http://127.0.0.1:8081
  api_key: "YOUR_MEDUSA_KEY"

ombi:
  url: http://127.0.0.1:5000
  api_key: "YOUR_OMBI_KEY"

web:
  secret_key: "RANDOM_SECRET"
  admin_password: "HASHED_PASSWORD"
  port: 9393

notifications:
  enabled: true
  method: email  # email | discord
  email:
    smtp_host: smtp.example.com
    smtp_port: 587
    smtp_user: ""
    smtp_pass: ""
    recipient: ""
  discord:
    webhook_url: ""

maintenance:
  dry_run: false
  log_file: /var/log/mediacleaner.log
```

## Data Model

### Rule

A rule is scoped to either a Plex library section (category) or an individual media item.

| Column | Type | Description |
|--------|------|-------------|
| id | int (PK) | |
| scope | enum | `category` or `media` |
| plex_library | str | Plex library name (for category rules) |
| plex_rating_key | str | Plex rating key (for media rules) |
| media_title | str | Human-readable title (display only) |
| action | enum | `keep` / `delete` |
| min_days_watched | int | Days after watched before eligible for deletion |
| max_days_age | int | Hard age limit (days since added), 0 = no limit |
| min_episodes | int | Never delete below this episode count (shows only) |
| watched_by | str | Comma-separated usernames whose watch status matters; "any" for any user |
| protect_on_deck | bool | Skip items currently on deck |
| enabled | bool | Rule active or not |
| created_at | datetime | |
| updated_at | datetime | |

### ActionLog

Records every action taken (or would-be-taken in dry run).

| Column | Type | Description |
|--------|------|-------------|
| id | int (PK) | |
| timestamp | datetime | |
| media_title | str | |
| plex_rating_key | str | |
| rule_id | int (FK) | Rule that triggered the action |
| action_taken | str | delete / keep / flag / orphan_detected |
| dry_run | bool | Whether this was a dry-run evaluation |
| details | text | JSON blob with reasoning |
| confirmed | bool | Whether deletion was confirmed post-action |

### ManagedMedia (cache)

A periodically-refreshed cache mapping media to its managing application.

| Column | Type | Description |
|--------|------|-------------|
| id | int (PK) | |
| plex_rating_key | str | |
| title | str | |
| plex_library | str | |
| manager | enum | `sonarr` / `radarr` / `medusa` / `none` |
| manager_id | int | ID within the managing application |
| file_path | str | Path on disk |
| last_synced | datetime | |

## Rule Resolution

1. Look for a **media-level** rule matching the item's `plex_rating_key`. If found and enabled, use it.
2. Look for a **show-level** rule matching the show's `plex_rating_key`. If found, evaluate per-episode (keep the newest `min_episodes`, delete watched episodes past `min_days_watched`).
3. Otherwise, look for a **category-level** rule matching the item's `plex_library`. If found and enabled, use it.
4. Otherwise, **default action is `keep`** (never delete without an explicit rule).

### Show-Level Rules

Show-scoped rules operate on individual episodes within a series:
- `min_episodes`: Always keep the N most recent episodes regardless of watch status.
- Remaining episodes are evaluated individually for watched status, grace period, etc.
- Episode deletion is done via Sonarr's episode file API (not removing the whole series).

## Maintenance Job Workflow

Runs daily (via cron/systemd timer). Steps:

1. **Sync managed media cache** — Query Sonarr, Radarr, Medusa for their managed series/movies. Update `ManagedMedia` table.
2. **Detect orphans** — Compare Plex library contents against `ManagedMedia`. Flag items with `manager = none`.
3. **Evaluate rules** — For each item in Plex:
   a. Resolve applicable rule (media → category → default keep).
   b. If action is `keep`, skip.
   c. Check conditions: watched_by, min_days_watched, max_days_age, min_episodes, protect_on_deck.
   d. If all conditions met, mark for deletion.
4. **Execute deletions** (unless dry_run):
   a. Delete via the managing application (Sonarr API `DELETE /series/{id}?deleteFiles=true`, Radarr equivalent, Medusa equivalent).
   b. Clean up Ombi — remove any request associated with the deleted media.
   c. Log action.
5. **Confirm deletions** — After a delay (or on next run), verify the files are gone from Plex. Update ActionLog.
6. **Send notifications** — Summary of actions taken (or would-be-taken in dry run).

## Deletion Flow Detail

```
Rule says DELETE
    │
    ├─ Manager is Sonarr → Sonarr API: DELETE /series/{id}?deleteFiles=true
    ├─ Manager is Radarr → Radarr API: DELETE /movie/{id}?deleteFiles=true  
    ├─ Manager is Medusa → Medusa API: remove show
    └─ Manager is None (orphan) → Direct filesystem delete + Plex library scan
    
    Then: Ombi API → remove matching request
    Then: Log action, await confirmation on next run
```

## Orphan Detection

An item is considered orphaned if:
- It exists in a Plex library
- It is NOT tracked by Sonarr, Radarr, or Medusa (matched by path or title)
- It has no "keep" rule applied

Orphans are flagged in the UI and in maintenance reports. They are not auto-deleted unless a rule explicitly covers them.

## Web UI

### Pages

| Route | Purpose |
|-------|---------|
| `/login` | Password login |
| `/` | Dashboard — summary stats, recent actions, orphan count |
| `/rules` | List all rules (category + media), create/edit/delete |
| `/rules/new` | Create rule form |
| `/rules/<id>/edit` | Edit rule form |
| `/media` | Browse Plex libraries, see which rule applies to each item |
| `/orphans` | List orphaned media with options to assign a rule or delete |
| `/log` | Action history with filtering |
| `/preview` | Dry-run preview — what would happen if maintenance ran now |

### Auth

Simple session-based auth. Single admin password stored as bcrypt hash in config.yaml. No user registration.

## Notifications

After each maintenance run, send a summary:
- Items deleted (with titles and reasons)
- New orphans detected
- Errors encountered
- Dry-run results (if in dry-run mode)

Supports email (SMTP) and Discord (webhook) initially.

## Future Considerations (out of scope for v1)

- Multi-user auth with per-user rule ownership
- Tautulli integration for richer watch analytics
- Webhook triggers (delete on demand, not just daily)
- Mobile-friendly UI improvements
- Backup/restore of rule database
