"""
Static application constants and logging setup.
Runtime settings (Kavita URL, API key, etc.) live in settings.py.
"""
import logging
import os
from pathlib import Path

# ── App auth ──────────────────────────────────────────────────────────────────
# The data directory is shared with settings.py so auth.json lives alongside settings.json
DATA_DIR         = Path(os.getenv("DATA_DIR", "data"))
AUTH_FILE        = DATA_DIR / "auth.json"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"

# ── Server ────────────────────────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8080"))

# ── Logging ───────────────────────────────────────────────────────────────────
# Console-only at import time; a file handler is added lazily on first use
# to avoid Windows asyncio/IocpProactor deadlocks during uvicorn startup.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

def _add_file_handler() -> None:
    """
    Attach a rotating FileHandler to the root logger.
    Must be called after the event loop is running to avoid Windows IocpProactor deadlocks.
    """
    import threading
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return

    def _open():
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(DATA_DIR / "kavita_ingest.log", delay=False)
            fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
            root.addHandler(fh)
        except Exception:
            pass

    # Run in a thread so the Windows IOCP proactor doesn't block the event loop
    threading.Thread(target=_open, daemon=True).start()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
