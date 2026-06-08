"""
Kavita Ingest Manager — application entrypoint.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import kavita
import settings
from config import PORT, _add_file_handler, get_logger
from routes.api import router as api_router
from routes.pages import router as pages_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        _add_file_handler()   # safe to open file after event loop is running
        logger.info("=" * 50)
        logger.info("Kavita Ingest Manager starting up...")
        logger.info("=" * 50)
        s = settings.load()
        if s.get("kavita_url") and s.get("kavita_api_key"):
            logger.info("Kavita configured — authenticating on startup...")
            await kavita.get_token()
        else:
            logger.info("Kavita not yet configured — visit /settings to connect.")
    except Exception as exc:
        logger.exception(f"Startup error: {exc}")
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(api_router)
app.include_router(pages_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
