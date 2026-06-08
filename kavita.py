"""
Kavita API client: authentication and library operations.
Reads kavita_url and kavita_api_key from settings at call-time so that
changes made in the /settings UI take effect without a restart.
"""
import httpx

import settings
from config import get_logger

logger = get_logger(__name__)

# Cached JWT token — invalidated whenever settings change
_jwt_token: str | None = None
_token_for_key: str | None = None   # the api_key the cached token was issued for


def invalidate_token() -> None:
    """Force re-authentication on the next API call."""
    global _jwt_token, _token_for_key
    _jwt_token = None
    _token_for_key = None


async def get_token() -> str | None:
    """
    Return a valid JWT token, re-authenticating if necessary.
    Returns None (without raising) if Kavita is not yet configured.
    """
    global _jwt_token, _token_for_key

    api_key = settings.get("kavita_api_key", "")
    url     = settings.get("kavita_url",     "")

    logger.info(f"get_token: api_key present={bool(api_key)}, url present={bool(url)}")
    if not api_key or not url:
        logger.warning("get_token: missing api_key or url")
        return None

    # Re-authenticate if the key has changed since last login
    if _jwt_token and _token_for_key == api_key:
        logger.info("get_token: using cached token")
        return _jwt_token

    logger.info("Authenticating with Kavita via Plugin API...")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"{url}/api/Plugin/authenticate",
                params={"apiKey": api_key, "pluginName": "KavitaIngest"},
                content=b"",
            )
        if resp.status_code == 200:
            token = resp.json().get("token")
            if token:
                _jwt_token = token
                _token_for_key = api_key
                logger.info("Kavita authentication successful")
                return _jwt_token
            logger.warning("Plugin API response missing token field")
        else:
            logger.warning(f"Kavita auth failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as exc:
        logger.warning(f"Kavita authentication error: {exc}")

    invalidate_token()
    return None


def _auth_headers() -> dict:
    if _jwt_token:
        return {"Authorization": f"Bearer {_jwt_token}", "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}


def _base_url() -> str:
    return settings.get("kavita_url", "").rstrip("/")


async def test_connection() -> dict:
    """
    Try to authenticate and return a status dict suitable for the UI.
    Does NOT use the token cache — always makes a fresh request.
    """
    invalidate_token()
    api_key = settings.get("kavita_api_key", "")
    url     = settings.get("kavita_url",     "")

    if not api_key or not url:
        return {"ok": False, "error": "Kavita URL and API key are required."}

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"{url}/api/Plugin/authenticate",
                params={"apiKey": api_key, "pluginName": "KavitaIngest"},
                content=b"",
            )
        if resp.status_code == 200 and resp.json().get("token"):
            # Warm the cache so the first real request is instant
            _jwt_token_val = resp.json()["token"]
            global _jwt_token, _token_for_key
            _jwt_token = _jwt_token_val
            _token_for_key = api_key
            username = resp.json().get("username", "")
            return {"ok": True, "username": username, "kavita_version": resp.json().get("kavitaVersion", "")}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def list_directory(path: str) -> list[str]:
    """
    Ask Kavita to list the contents of a directory *from its own perspective*.
    Returns a list of full paths, or [] on error.
    Used to verify that Kavita can see the same folders it reports in its library config.
    """
    token = await get_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_base_url()}/api/Library/list",
                params={"path": path},
                headers=_auth_headers(),
            )
        if resp.status_code == 200:
            return [d.get("fullPath", "") for d in resp.json()]
        return []
    except Exception as exc:
        logger.warning(f"list_directory({path!r}) error: {exc}")
        return []


async def fetch_libraries() -> list:
    """Return the list of Kavita libraries (empty list if unconfigured)."""
    logger.info("fetch_libraries: starting")
    token = await get_token()
    logger.info(f"fetch_libraries: token obtained: {bool(token)}")
    if not token:
        logger.warning("fetch_libraries: no token - returning empty list")
        return []
    try:
        url = f"{_base_url()}/api/Library/libraries"
        logger.info(f"fetch_libraries: requesting {url}")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers=_auth_headers(),
            )
        logger.info(f"fetch_libraries: response status {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"fetch_libraries: returned {len(data) if isinstance(data, list) else type(data)} libraries")
            return data
        if resp.status_code == 204:
            logger.info("fetch_libraries: 204 No Content - returning empty list")
            return []
        logger.warning(f"fetch_libraries: unexpected status {resp.status_code}, body: {resp.text[:200]}")
        return []
    except Exception as exc:
        logger.error(f"fetch_libraries error: {exc}", exc_info=True)
        return []


async def scan_folder(kavita_folder: str) -> bool:
    """
    Trigger a targeted scan of a specific folder using /api/Library/scan-folder.

    This is faster and more precise than a full library scan — Kavita only
    processes the folder we just wrote to rather than re-walking the entire library.

    The folderPath sent here is the path Kavita itself knows about (the value from
    library.folders[], not the host-side books_root path).

    Returns True if the scan was accepted (HTTP 200), False otherwise.
    """
    token = await get_token()
    if not token:
        logger.warning("scan_folder: Kavita not configured, skipping")
        return False
    api_key = settings.get("kavita_api_key", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_base_url()}/api/Library/scan-folder",
                json={
                    "apiKey": api_key,
                    "folderPath": kavita_folder,
                    "abortOnNoSeriesMatch": False,
                },
                headers=_auth_headers(),
            )
        if resp.status_code == 200:
            logger.info(f"scan-folder accepted for: {kavita_folder}")
            return True
        logger.warning(f"scan-folder {kavita_folder}: HTTP {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as exc:
        logger.warning(f"scan-folder failed for {kavita_folder}: {exc}")
        return False


async def scan_library(library_id: int) -> None:
    """
    Trigger a full Kavita library scan.

    Prefer scan_folder() for targeted post-upload scans — this is the fallback
    when no specific folder path is available.
    """
    token = await get_token()
    if not token:
        logger.warning("scan_library: Kavita not configured, skipping scan")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_base_url()}/api/Library/scan",
                params={"libraryId": library_id},
                headers=_auth_headers(),
            )
        logger.info(f"scan library {library_id}: HTTP {resp.status_code}")
    except Exception as exc:
        logger.warning(f"scan_library failed for {library_id}: {exc}")
