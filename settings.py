"""
Persistent application settings stored in data/settings.json.

All values can be changed at runtime via the /settings UI and are written
immediately to disk so they survive restarts.

Environment variables are only used as *initial defaults* the very first time
the settings file is created — they are never re-read after that.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from config import get_logger

logger = get_logger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR      = Path(os.getenv("DATA_DIR", "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

# ── Defaults (used only when no settings file exists yet) ─────────────────────
_DEFAULTS: dict[str, Any] = {
    "kavita_url":     os.getenv("KAVITA_URL",     ""),
    "kavita_api_key": os.getenv("KAVITA_API_KEY", ""),
    # Root folder on THIS machine used to auto-detect per-library paths.
    # e.g. \\FamilyNAS\kavita\books  /mnt/books  /books
    "books_root":     os.getenv("BOOKS_ROOT",     ""),
    # Per-library host paths, keyed by Kavita library ID (stored as string).
    # { "3": "\\\\NAS\\books\\Physics", "7": "/mnt/books/AI" }
    # Populated automatically by detect-paths, editable per row in Settings UI.
    "library_paths":  {},
    # Enable/disable Open Library metadata lookup
    "enable_open_library": True,
    # LLM provider configuration
    "llm_provider": "deepseek",
    "llm_model": "deepseek-chat",
    "llm_api_key": "",
    "llm_base_url": "https://api.deepseek.com/v1",
    # Preferred subjects for auto-categorization (user can add/edit these)
    "preferred_subjects": [
        "Physics", "Chemistry", "Biology", "Mathematics", "Computer Science",
        "Artificial Intelligence", "Machine Learning", "Data Science",
        "Engineering", "Economics", "Psychology", "Philosophy",
        "History", "Literature", "Fiction", "Non-Fiction"
    ],
}


# ── Internal cache ────────────────────────────────────────────────────────────
_cache: dict[str, Any] | None = None


def _load_from_disk() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        logger.info("No settings file found — creating with defaults")
        _save_to_disk(_DEFAULTS.copy())
        return _DEFAULTS.copy()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        # Merge in any keys added in newer versions of the app
        changed = False
        for k, v in _DEFAULTS.items():
            if k not in data:
                data[k] = v
                changed = True
        if changed:
            _save_to_disk(data)
        return data
    except Exception as exc:
        logger.error(f"Failed to read settings file: {exc} — using defaults")
        return _DEFAULTS.copy()


def _save_to_disk(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load() -> dict[str, Any]:
    """Return the current settings dict (cached in-process)."""
    global _cache
    if _cache is None:
        _cache = _load_from_disk()
    return _cache


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """Merge *updates* into the current settings, persist, and return the new dict."""
    global _cache
    current = load()
    current.update(updates)
    _save_to_disk(current)
    _cache = current
    logger.info(f"Settings saved: {list(updates.keys())}")
    return current


def is_kavita_configured() -> bool:
    s = load()
    return bool(s.get("kavita_url") and s.get("kavita_api_key"))


def _normalise_books_root(raw: str) -> str:
    """
    Normalise a user-supplied books root path so it works on both Windows and Linux.

    Handles these forms that users commonly paste:
      - Correct Windows UNC:          backslash backslash server backslash share
      - Double-escaped from docs:     four backslashes server (paste artifact)
      - Forward-slash UNC:            //server/share  (also valid on Windows SMB)
      - Linux / Docker:               /mnt/books or /books  (unchanged)
      - Local Windows drive:          C:backslashbooks  (unchanged)
    """
    s = raw.strip()
    if not s:
        return s

    on_windows = sys.platform == "win32"

    # Forward-slash UNC (//server/share) → backslash UNC on Windows
    if on_windows and s.startswith("//"):
        s = "\\\\" + s[2:].replace("/", "\\")

    # Collapse double-escaped UNC: \\\\server\\share → \\server\share
    # Users paste these from Python repr output or README code blocks.
    if on_windows and s.startswith("\\\\"):
        # Strip all leading backslashes, then re-add exactly two
        stripped = s.lstrip("\\")
        # Collapse any remaining double-backslash runs in the interior
        stripped = re.sub(r"\\\\+", "\\\\", stripped)
        s = "\\\\" + stripped

    return s


def resolve_library_path(kavita_folder: str, library_id: int | str | None = None) -> str:
    """
    Return the host-accessible path for a Kavita library folder.

    Resolution order:
      1. Per-library explicit mapping  (library_paths[str(library_id)])
      2. books_root + last segment of kavita_folder  (auto fallback)
      3. kavita_folder unchanged  (nothing configured — caller detects empty)

    Path normalisation (_normalise_books_root) handles UNC double-escaping,
    forward-slash UNC, and whitespace on Windows.
    """
    s = load()
    library_paths: dict = s.get("library_paths") or {}

    # 1. Explicit per-library mapping
    if library_id is not None:
        explicit = library_paths.get(str(library_id), "").strip()
        if explicit:
            return _normalise_books_root(explicit)

    # 2. books_root + subfolder name
    raw_root = (s.get("books_root") or "").strip()
    if raw_root:
        books_root = _normalise_books_root(raw_root)
        subfolder = Path(kavita_folder.replace("\\", "/")).name
        if subfolder:
            return os.path.join(books_root, subfolder)

    # 3. Nothing configured
    return ""


def get_library_path(library_id: int | str) -> str:
    """Return the saved explicit host path for a library ID, or ''."""
    lp = (load().get("library_paths") or {})
    return lp.get(str(library_id), "").strip()


def save_library_paths(mapping: dict[str, str]) -> None:
    """
    Persist per-library path mappings.
    mapping = { "3": "/mnt/books/Physics", "7": "/mnt/books/AI", ... }
    Values are normalised before saving.
    """
    normalised = {
        str(k): _normalise_books_root(v.strip())
        for k, v in mapping.items()
        if v.strip()
    }
    save({"library_paths": normalised})
