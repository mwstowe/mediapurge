from datetime import datetime, timezone
from functools import lru_cache
import time

from plexapi.server import PlexServer

from mediapurge.config import get_config


def _to_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to UTC-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _timed_lru_cache(seconds=300, maxsize=128):
    """LRU cache with TTL expiry."""
    def decorator(func):
        func = lru_cache(maxsize=maxsize)(func)
        func._expiry = time.time() + seconds
        func._ttl = seconds
        original = func.__wrapped__

        def wrapper(*args, **kwargs):
            if time.time() > func._expiry:
                func.cache_clear()
                func._expiry = time.time() + func._ttl
            return func(*args, **kwargs)

        wrapper.cache_clear = func.cache_clear
        return wrapper
    return decorator


_plex_server = None
_plex_server_time = 0


def _server() -> PlexServer:
    global _plex_server, _plex_server_time
    now = time.time()
    if _plex_server is None or (now - _plex_server_time) > 600:
        cfg = get_config()["plex"]
        _plex_server = PlexServer(cfg["url"], cfg["token"])
        _plex_server_time = now
    return _plex_server


def get_users() -> list[dict]:
    """Return all Plex home/managed users with name and email, plus any extra from config."""
    cfg = get_config()["plex"]
    email_map = cfg.get("user_emails", {})
    server = _server()
    account = server.myPlexAccount()
    seen = set()
    users = []

    # Plex account owner
    users.append({"username": account.username, "email": email_map.get(account.username, account.email)})
    seen.add(account.username)

    # Plex shared/home users
    for user in account.users():
        name = user.username or user.title
        users.append({"username": name, "email": email_map.get(name, user.email or "")})
        seen.add(name)

    # Any extra entries in user_emails that aren't Plex users
    for name, email in email_map.items():
        if name not in seen:
            users.append({"username": name, "email": email})

    return users


@_timed_lru_cache(seconds=300)
def get_libraries():
    return [(s.title, s.type) for s in _server().library.sections()]


def scan_library(library_name: str):
    """Trigger a Plex library scan."""
    _server().library.section(library_name).update()


@_timed_lru_cache(seconds=120)
def _get_system_accounts():
    return {a.id: a.name for a in _server().systemAccounts()}


@_timed_lru_cache(seconds=300)
def get_manager_info():
    """Build a lookup of file path -> {managers, ended} from Sonarr, Radarr, Medusa."""
    from mediapurge.clients import sonarr, radarr, medusa
    import warnings
    warnings.filterwarnings("ignore")
    info = {}

    try:
        for s in sonarr.get_all_series():
            path = s["path"]
            info.setdefault(path, {"managers": [], "ended": None})
            info[path]["managers"].append("Sonarr")
            info[path]["ended"] = s.get("ended", s.get("status") == "ended")
    except Exception:
        pass

    try:
        for m in radarr.get_all_movies():
            path = m.get("path", "")
            if path:
                info.setdefault(path, {"managers": [], "ended": None})
                info[path]["managers"].append("Radarr")
    except Exception:
        pass

    try:
        for s in medusa.get_all_shows():
            path = s.get("config", {}).get("location", "")
            if path:
                info.setdefault(path, {"managers": [], "ended": None})
                info[path]["managers"].append("Medusa")
                status = s.get("status", "")
                if status:
                    info[path]["ended"] = status.lower() == "ended"
    except Exception:
        pass

    return info


def get_library_items(library_name: str):
    """Return all items in a library section."""
    return _server().library.section(library_name).all()


def is_watched_by(item, usernames: list[str]) -> tuple[bool, datetime | None]:
    """Check if item is watched by any of the given users. Returns (watched, last_viewed_at)."""
    cfg = get_config()["plex"]
    server_url = cfg["url"]
    user_tokens = cfg.get("user_tokens", {})

    for username in usernames:
        token = user_tokens.get(username, cfg["token"])
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
    # Shows/seasons use .locations
    locations = getattr(item, "locations", None)
    if locations:
        return list(locations)
    # Movies/episodes use .media.parts
    paths = []
    for media in getattr(item, "media", []):
        for part in media.parts:
            paths.append(part.file)
    return paths


def get_file_size(item) -> int:
    """Return total file size in bytes for a media item."""
    total = 0
    for media in getattr(item, "media", []):
        for part in media.parts:
            total += getattr(part, "size", 0) or 0
    if not total:
        import os
        for path in get_file_paths(item):
            try:
                if os.path.isdir(path):
                    for root, _, files in os.walk(path):
                        for f in files:
                            total += os.path.getsize(os.path.join(root, f))
                elif os.path.exists(path):
                    total += os.path.getsize(path)
            except OSError:
                pass
    return total


def days_since_watched(last_viewed_at: datetime | None) -> int | None:
    if last_viewed_at is None:
        return None
    return (datetime.now(timezone.utc) - _to_utc(last_viewed_at)).days


@_timed_lru_cache(seconds=300)
def _get_recent_history():
    """Fetch recent watch history for the whole server in one call.
    Indexes by episode key AND show key so both levels can look up."""
    history = {}
    accounts = _get_system_accounts()
    for h in _server().history(maxresults=5000):
        entry = {
            "viewed_at": getattr(h, "viewedAt", None),
            "viewed_by": accounts.get(getattr(h, "accountID", None)),
        }
        key = str(h.ratingKey)
        if key not in history:
            history[key] = entry
        # Also index by show (grandparent) key for browse-level lookups
        gp_key_path = getattr(h, "grandparentKey", None)
        if gp_key_path:
            gp_key = gp_key_path.rsplit("/", 1)[-1]
            if gp_key not in history:
                history[gp_key] = entry
    return history


def get_last_viewed_info(item) -> dict:
    """Get last viewed date and which user viewed it for a media item."""
    history = _get_recent_history()
    key = str(item.ratingKey)
    if key in history:
        return history[key]
    # Fallback to item's own lastViewedAt (no user info)
    viewed_at = getattr(item, "lastViewedAt", None)
    return {"viewed_at": viewed_at, "viewed_by": None}


def days_since_last_activity(show) -> int | None:
    """Return days since the most recent watch of any episode in a show."""
    latest = None
    for ep in show.episodes():
        viewed = getattr(ep, "lastViewedAt", None)
        if viewed is not None:
            viewed = _to_utc(viewed)
            if latest is None or viewed > latest:
                latest = viewed
    if latest is None:
        return None
    return (datetime.now(timezone.utc) - latest).days


def days_since_added(item) -> int:
    return (datetime.now(timezone.utc) - _to_utc(item.addedAt)).days


def all_episodes_watched_by(show, usernames: list[str]) -> tuple[bool, datetime | None]:
    """Check if ALL episodes of a show are watched by the specified users.
    Returns (all_watched, date_last_episode_was_watched)."""
    latest_viewed = None
    for ep in show.episodes():
        if "any" in usernames:
            if not ep.isWatched:
                return False, None
            viewed = _to_utc(getattr(ep, "lastViewedAt", None))
        else:
            watched, viewed = is_watched_by(ep, usernames)
            if not watched:
                return False, None
            viewed = _to_utc(viewed)
        if viewed and (latest_viewed is None or viewed > latest_viewed):
            latest_viewed = viewed
    return True, latest_viewed


def get_move_destinations() -> list[dict]:
    """Return all known root folders with manager and Plex library info."""
    from mediapurge.clients import sonarr, radarr, medusa
    import warnings
    warnings.filterwarnings("ignore")

    # Get Plex library → path mapping
    lib_paths = {}
    lib_types = {}
    try:
        server = _server()
        for section in server.library.sections():
            for loc in section.locations:
                lib_paths[loc.rstrip("/")] = section.title
                lib_types[loc.rstrip("/")] = section.type
    except Exception:
        pass

    destinations = {}  # path -> {managers: [], plex_library: str}

    try:
        for f in sonarr.get_root_folders():
            p = f.rstrip("/")
            destinations.setdefault(p, {"managers": [], "plex_library": None})
            destinations[p]["managers"].append("Sonarr")
    except Exception:
        pass
    try:
        for f in radarr.get_root_folders():
            p = f.rstrip("/")
            destinations.setdefault(p, {"managers": [], "plex_library": None})
            destinations[p]["managers"].append("Radarr")
    except Exception:
        pass
    try:
        for f in medusa.get_root_folders():
            p = f.rstrip("/")
            destinations.setdefault(p, {"managers": [], "plex_library": None})
            destinations[p]["managers"].append("Medusa")
    except Exception:
        pass

    # Match paths to Plex libraries
    for path, info in destinations.items():
        for lib_path, lib_name in lib_paths.items():
            if path == lib_path or lib_path.startswith(path + "/") or path.startswith(lib_path + "/"):
                info["plex_library"] = lib_name
                info["lib_type"] = lib_types.get(lib_path, "")
                break

    # Produce one entry per manager+path so user explicitly chooses which manager
    # Sort: shows first, then movies, alphabetically within each group
    result = []
    for p, d in sorted(destinations.items()):
        for mgr in d["managers"]:
            result.append({
                "value": f"{mgr.lower()}:{p}",
                "path": p,
                "manager": mgr,
                "plex_library": d["plex_library"] or "—",
                "lib_type": d.get("lib_type", ""),
            })
    result.sort(key=lambda x: (0 if x["lib_type"] == "show" else 1, x["path"]))
    return result
