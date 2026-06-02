from datetime import datetime, timezone

from plexapi.server import PlexServer

from mediacleaner.config import get_config


def _server() -> PlexServer:
    cfg = get_config()["plex"]
    return PlexServer(cfg["url"], cfg["token"])


def get_users() -> list[dict]:
    """Return all Plex home/managed users with name and email."""
    cfg = get_config()["plex"]
    email_map = cfg.get("user_emails", {})
    server = _server()
    account = server.myPlexAccount()
    users = [{"username": account.username, "email": email_map.get(account.username, account.email)}]
    for user in account.users():
        name = user.username or user.title
        users.append({"username": name, "email": email_map.get(name, user.email or "")})
    return users


def get_libraries() -> list[str]:
    return [s.title for s in _server().library.sections()]


def get_library_items(library_name: str):
    """Return all items in a library section."""
    return _server().library.section(library_name).all()


def is_watched_by(item, usernames: list[str]) -> tuple[bool, datetime | None]:
    """Check if item is watched by any of the given users. Returns (watched, last_viewed_at)."""
    cfg = get_config()["plex"]
    server_url = cfg["url"]

    for username in usernames:
        token = cfg.get("tokens", {}).get(username, cfg["token"])
        user_server = PlexServer(server_url, token)
        try:
            user_item = user_server.fetchItem(item.ratingKey)
            if user_item.isWatched:
                viewed = getattr(user_item, "lastViewedAt", None)
                return True, viewed
        except Exception:
            continue
    return False, None


def is_on_deck(item) -> bool:
    """Check if an item is currently on a user's on-deck list."""
    server = _server()
    on_deck_keys = {ep.ratingKey for ep in server.library.onDeck()}
    # For shows, check if any episode is on deck
    if hasattr(item, "episodes"):
        return any(ep.ratingKey in on_deck_keys for ep in item.episodes())
    return item.ratingKey in on_deck_keys


def get_episode_count(show) -> int:
    """Return total episode count for a show."""
    return len(show.episodes())


def get_file_paths(item) -> list[str]:
    """Return file paths for a media item."""
    paths = []
    for media in getattr(item, "media", []):
        for part in media.parts:
            paths.append(part.file)
    return paths


def days_since_watched(last_viewed_at: datetime | None) -> int | None:
    if last_viewed_at is None:
        return None
    now = datetime.now(timezone.utc)
    if last_viewed_at.tzinfo is None:
        last_viewed_at = last_viewed_at.replace(tzinfo=timezone.utc)
    return (now - last_viewed_at).days


def get_last_viewed_info(item) -> dict:
    """Get last viewed date and which user viewed it for a media item."""
    viewed_at = getattr(item, "lastViewedAt", None)
    viewed_by = None
    try:
        history = item.history()
        if history:
            latest = history[0]
            account_id = getattr(latest, "accountID", None)
            if not viewed_at:
                viewed_at = getattr(latest, "viewedAt", None)
            if account_id is not None:
                server = _server()
                for sa in server.systemAccounts():
                    if sa.id == account_id:
                        viewed_by = sa.name
                        break
    except Exception:
        pass
    return {"viewed_at": viewed_at, "viewed_by": viewed_by}


def days_since_last_activity(show) -> int | None:
    """Return days since the most recent watch of any episode in a show."""
    latest = None
    for ep in show.episodes():
        viewed = getattr(ep, "lastViewedAt", None)
        if viewed is not None:
            if latest is None or viewed > latest:
                latest = viewed
    if latest is None:
        return None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - latest).days


def days_since_added(item) -> int:
    added = item.addedAt
    if added.tzinfo is None:
        added = added.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - added).days


def all_episodes_watched_by(show, usernames: list[str]) -> tuple[bool, datetime | None]:
    """Check if ALL episodes of a show are watched by the specified users.
    Returns (all_watched, date_last_episode_was_watched)."""
    latest_viewed = None
    for ep in show.episodes():
        if "any" in usernames:
            if not ep.isWatched:
                return False, None
            viewed = getattr(ep, "lastViewedAt", None)
        else:
            watched, viewed = is_watched_by(ep, usernames)
            if not watched:
                return False, None
        if viewed and (latest_viewed is None or viewed > latest_viewed):
            latest_viewed = viewed
    return True, latest_viewed
