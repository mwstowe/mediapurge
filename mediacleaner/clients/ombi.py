import requests

from mediacleaner.config import get_config


def _base() -> tuple[str, dict]:
    cfg = get_config()["ombi"]
    return cfg["url"].rstrip("/"), {"ApiKey": cfg["api_key"]}


def get_movie_requests() -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v1/Request/movie", headers=headers)
    r.raise_for_status()
    return r.json()


def get_tv_requests() -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v1/Request/tv", headers=headers)
    r.raise_for_status()
    return r.json()


def delete_movie_request(request_id: int):
    url, headers = _base()
    r = requests.delete(f"{url}/api/v1/Request/movie/{request_id}", headers=headers)
    r.raise_for_status()


def delete_tv_request(request_id: int):
    url, headers = _base()
    r = requests.delete(f"{url}/api/v1/Request/tv/{request_id}", headers=headers)
    r.raise_for_status()


def cleanup_for_title(title: str):
    """Remove any Ombi requests matching the given title."""
    for req in get_movie_requests():
        if req.get("title", "").lower() == title.lower():
            delete_movie_request(req["id"])
    for req in get_tv_requests():
        if req.get("title", "").lower() == title.lower():
            delete_tv_request(req["id"])


def approve_managed_requests(managed_titles: set[str]):
    """Approve Ombi requests that are already managed by an arr."""
    url, headers = _base()
    approved = []

    for req in get_movie_requests():
        if not req.get("approved") and req.get("title", "").lower() in managed_titles:
            r = requests.post(f"{url}/api/v1/Request/movie/approve", headers=headers, json={"id": req["id"]})
            if r.status_code == 200:
                approved.append(req["title"])

    for req in get_tv_requests():
        for child in req.get("childRequests", []):
            if not child.get("approved") and req.get("title", "").lower() in managed_titles:
                r = requests.post(f"{url}/api/v1/Request/tv/approve", headers=headers, json={"id": child["id"]})
                if r.status_code == 200:
                    approved.append(req["title"])
                break

    return approved
