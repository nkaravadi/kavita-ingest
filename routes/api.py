"""
All /api/* endpoints.
"""
import json
import os
from pathlib import Path
import re
from typing import List

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import kavita
import metadata as md
import settings as app_settings
from settings import resolve_library_path, save_library_paths
from auth import (
    authenticate_user,
    get_current_user,
    hash_password,
    load_users,
    make_session_token,
    require_auth,
    save_users,
    verify_password,
)
from config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api")


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_name(name: str) -> str:
    return re.sub(r'[\/:*?"<>|]', " - ", name).strip()


async def _write_file(file: UploadFile, target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            fh.write(chunk)


# ── Auth ──────────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    logger.info(f"Login attempt for user: {username}")
    if not authenticate_user(username, password):
        logger.warning(f"Login failed for user: {username}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    users = load_users()
    token = make_session_token(username, users[username])
    logger.info(f"Login successful for user: {username}")
    resp = JSONResponse(content={"status": "success", "username": username})
    resp.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse(content={"status": "success"})
    resp.delete_cookie("session_token")
    return resp


@router.post("/change-password")
async def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    current_user: str = Depends(require_auth),
):
    users = load_users()
    if not verify_password(current_password, users[current_user]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    users[current_user] = hash_password(new_password)
    save_users(users)
    return {"status": "success", "message": "Password changed successfully"}


# ── Settings ──────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    kavita_url:    str = ""
    kavita_api_key: str = ""
    books_root:    str = ""
    enable_open_library: bool = True
    llm_provider:  str = ""
    llm_model:     str = ""
    llm_api_key:   str = ""
    llm_base_url:  str = ""
    preferred_subjects: list = []


@router.get("/settings")
async def get_settings(current_user: str = Depends(require_auth)):
    s = app_settings.load()
    raw_key = s.get("kavita_api_key", "")
    masked  = ("*" * max(0, len(raw_key) - 4) + raw_key[-4:]) if raw_key else ""
    return {
        "kavita_url":            s.get("kavita_url", ""),
        "kavita_api_key_masked": masked,
        "books_root":            s.get("books_root", ""),
        "library_paths":         s.get("library_paths", {}),
        "enable_open_library":   s.get("enable_open_library", True),
        "llm_provider":          s.get("llm_provider", "deepseek"),
        "llm_model":             s.get("llm_model", "deepseek-chat"),
        "llm_api_key":           s.get("llm_api_key", ""),
        "llm_base_url":          s.get("llm_base_url", "https://api.deepseek.com/v1"),
        "preferred_subjects":    s.get("preferred_subjects", []),
        "kavita_configured":     app_settings.is_kavita_configured(),
    }


@router.post("/settings")
async def save_settings(
    payload: SettingsPayload,
    current_user: str = Depends(require_auth),
):
    updates: dict = {}
    if payload.kavita_url:
        updates["kavita_url"] = payload.kavita_url.strip()
    if payload.books_root:
        updates["books_root"] = payload.books_root.strip()
    updates["enable_open_library"] = payload.enable_open_library
    if payload.llm_provider:
        updates["llm_provider"] = payload.llm_provider
    if payload.llm_model:
        updates["llm_model"] = payload.llm_model
    if payload.llm_api_key:
        updates["llm_api_key"] = payload.llm_api_key.strip()
    if payload.llm_base_url:
        updates["llm_base_url"] = payload.llm_base_url.strip()
    if payload.preferred_subjects:
        updates["preferred_subjects"] = payload.preferred_subjects
    # Only overwrite the API key if the user typed a real new value (not the masked placeholder)
    if payload.kavita_api_key and "*" not in payload.kavita_api_key:
        updates["kavita_api_key"] = payload.kavita_api_key.strip()
    if not updates:
        return {"status": "saved"}  # Nothing to update
    app_settings.save(updates)
    kavita.invalidate_token()
    return {"status": "saved"}


@router.post("/settings/test-connection")
async def test_connection(current_user: str = Depends(require_auth)):
    result = await kavita.test_connection()
    return result


@router.post("/settings/test-llm")
async def test_llm(current_user: str = Depends(require_auth)):
    """Test LLM connection (costs a tiny amount - makes real API call)"""
    llm_key = app_settings.get("llm_api_key", "").strip()
    if not llm_key:
        return {"status": "not_configured", "message": "LLM API key not configured"}
    
    try:
        from openai import AsyncOpenAI
        
        llm_provider = app_settings.get("llm_provider", "deepseek")
        llm_model = app_settings.get("llm_model", "deepseek-chat")
        llm_base_url = app_settings.get("llm_base_url", "https://api.deepseek.com/v1")
        
        client_kwargs = {"api_key": llm_key}
        if llm_provider == "deepseek":
            client_kwargs["base_url"] = llm_base_url
        
        client = AsyncOpenAI(**client_kwargs)
        
        # Make a simple test call (costs ~0.001 tokens)
        response = await client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=5,
        )
        
        return {
            "status": "ok",
            "provider": llm_provider,
            "model": llm_model,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


class LibraryPathsPayload(BaseModel):
    # { "3": "/mnt/books/Physics", "7": "" }  — empty string removes the mapping
    paths: dict[str, str]


@router.post("/settings/library-paths")
async def save_library_paths_endpoint(
    payload: LibraryPathsPayload,
    current_user: str = Depends(require_auth),
):
    """Save explicit per-library host path mappings."""
    save_library_paths(payload.paths)
    return {"status": "saved"}


def _check_host_path(host_folder: str) -> tuple[bool, list[str], str | None]:
    """
    Check whether host_folder is accessible and return
    (accessible: bool, subfolders: list[str], error: str | None).
    """
    if not host_folder:
        return False, [], "No path configured for this library"
    try:
        if not os.path.isdir(host_folder):
            return False, [], f"Directory not found: {host_folder}"
        subfolders = sorted(e.name for e in os.scandir(host_folder) if e.is_dir())
        return True, subfolders, None
    except Exception as exc:
        return False, [], str(exc)


@router.get("/settings/check-paths")
async def check_paths(current_user: str = Depends(require_auth)):
    """
    Per-library path check. For each Kavita library:
      - Resolves the configured host path (explicit mapping → books_root fallback)
      - Asks Kavita what it sees inside its own folder (via /api/Library/list)
      - Checks what this app sees at the resolved host path
      - Returns both views so mismatches are immediately visible
    """
    libs = await kavita.fetch_libraries()
    if not libs:
        return {"ok": False, "error": "No libraries — check Kavita connection.", "libraries": []}

    results = []
    all_ok = True

    for lib in libs:
        lib_id  = lib.get("id")
        lib_name = lib.get("name", "?")
        kavita_folder = (lib.get("folders") or [""])[0]

        host_folder = resolve_library_path(kavita_folder, library_id=lib_id)

        # What Kavita sees inside its own folder
        kavita_children  = await kavita.list_directory(kavita_folder) if kavita_folder else []
        kavita_subfolders = sorted(Path(p).name for p in kavita_children if p)

        # What this app sees
        accessible, host_subfolders, host_error = _check_host_path(host_folder)

        # A library is OK when both sides are accessible and their contents match.
        # Empty-on-both is fine for a new library (nothing uploaded yet).
        lib_ok = accessible and set(kavita_subfolders) == set(host_subfolders)
        if not lib_ok:
            all_ok = False

        entry: dict = {
            "id":            lib_id,
            "library":       lib_name,
            "kavita_folder": kavita_folder,
            "host_folder":   host_folder,
            "ok":            lib_ok,
            "kavita_sees":   kavita_subfolders,
            "host_sees":     host_subfolders,
        }
        if host_error:
            entry["host_error"] = host_error
        if accessible and not lib_ok:
            only_kavita = sorted(set(kavita_subfolders) - set(host_subfolders))
            only_host   = sorted(set(host_subfolders)   - set(kavita_subfolders))
            if only_kavita: entry["only_in_kavita"] = only_kavita
            if only_host:   entry["only_on_host"]   = only_host

        results.append(entry)

    return {"ok": all_ok, "libraries": results}


@router.post("/settings/rescan-all")
async def rescan_all_libraries(current_user: str = Depends(require_auth)):
    """
    Trigger a full scan of all Kavita libraries.
    Useful for manually refreshing Kavita's library database after uploads.
    """
    libs = await kavita.fetch_libraries()
    if not libs:
        return {"ok": False, "error": "No libraries found"}
    
    results = []
    for lib in libs:
        lib_id = lib.get("id")
        lib_name = lib.get("name", "")
        try:
            await kavita.scan_library(lib_id)
            results.append({"id": lib_id, "library": lib_name, "status": "scanned"})
            logger.info(f"Rescanned library: {lib_name} (ID: {lib_id})")
        except Exception as exc:
            results.append({"id": lib_id, "library": lib_name, "status": "error", "error": str(exc)})
            logger.error(f"Failed to rescan library {lib_name}: {exc}")
    
    return {"ok": True, "results": results}


@router.post("/settings/generate-subjects")
async def generate_subjects(current_user: str = Depends(require_auth)):
    """
    Generate a list of common book subjects using DeepSeek.
    Useful for building the preferred_subjects list.
    """
    llm_key = app_settings.get("llm_api_key", "").strip()
    if not llm_key:
        return {"ok": False, "error": "LLM API key not configured"}
    
    try:
        prompt = """Generate a comprehensive list of 30-50 common book subjects/categories for organizing a library. These should cover:
- Academic subjects (Physics, Chemistry, Mathematics, etc.)
- Technical fields (Computer Science, Engineering, etc.)
- Humanities (History, Philosophy, Literature, etc.)
- Genres (Fiction, Non-Fiction, Mystery, etc.)
- Specialized topics (Artificial Intelligence, Machine Learning, etc.)

Return ONLY a JSON array of strings, no markdown, no other text."""
        
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {deepseek_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a library organization assistant. Always return valid JSON arrays of strings. No markdown, no explanations."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    "temperature": 0.5,
                },
            )
        content = response.choices[0].message.content
        import json
        import re
        try:
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                subjects = json.loads(json_match.group())
                if isinstance(subjects, list) and all(isinstance(s, str) for s in subjects):
                    return {"ok": True, "subjects": subjects[:50]}
        except Exception as e:
            logger.error(f"Failed to parse DeepSeek subjects response: {e}")
        
        return {"ok": False, "error": "Failed to parse subjects from DeepSeek"}
    except Exception as exc:
        logger.error(f"Generate subjects failed: {exc}", exc_info=True)
        return {"ok": False, "error": str(exc)}


@router.get("/settings/detect-paths")
async def detect_paths(current_user: str = Depends(require_auth)):
    """
    Auto-detect host paths for every library by listing books_root on the
    host side and fuzzy-matching subfolder names to library names.

    Matching strategy (in order):
      1. Exact match (case-insensitive): library name == folder name
      2. Slugified match: normalise both sides (lower, collapse spaces/dashes/underscores)
      3. Substring match: folder name is contained in the library name or vice-versa

    Returns a proposal list — nothing is saved until the user confirms.
    """
    logger.info("detect-paths: starting")
    books_root_raw = (app_settings.get("books_root") or "").strip()
    logger.info(f"detect-paths: books_root_raw = {repr(books_root_raw)}")
    libs = await kavita.fetch_libraries()
    logger.info(f"detect-paths: fetched {len(libs)} libraries from Kavita")

    if not libs:
        logger.warning("detect-paths: no libraries returned from Kavita")
        return {"ok": False, "error": "No libraries — check Kavita connection.", "proposals": []}

    # List subfolders available under books_root
    host_subfolders: list[str] = []
    books_root = ""
    scan_error: str | None = None
    if books_root_raw:
        from settings import _normalise_books_root
        books_root = _normalise_books_root(books_root_raw)
        logger.info(f"detect-paths: normalised books_root = {repr(books_root)}")
        try:
            is_dir = os.path.isdir(books_root)
            logger.info(f"detect-paths: os.path.isdir({repr(books_root)}) = {is_dir}")
            if is_dir:
                host_subfolders = [e.name for e in os.scandir(books_root) if e.is_dir()]
                logger.info(f"detect-paths: found {len(host_subfolders)} subfolders: {host_subfolders}")
            else:
                scan_error = f"Directory does not exist or is not accessible: {books_root}"
                logger.warning(f"detect-paths: {scan_error}")
        except PermissionError as e:
            scan_error = f"Permission denied: {e}"
            logger.error(f"detect-paths: PermissionError: {e}")
        except OSError as e:
            scan_error = f"OS error: {e}"
            logger.error(f"detect-paths: OSError: {e}")
        except Exception as e:
            scan_error = f"Unexpected error: {e}"
            logger.error(f"detect-paths: Unexpected error: {e}", exc_info=True)
    else:
        logger.info("detect-paths: books_root_raw is empty - no root configured")

    existing = app_settings.get("library_paths") or {}

    def slug(s: str) -> str:
        return re.sub(r"[\s_\-]+", "", s.lower())

    proposals = []
    for lib in libs:
        lib_id   = str(lib.get("id"))
        lib_name = lib.get("name", "")
        kavita_folder = (lib.get("folders") or [""])[0]
        lib_slug = slug(lib_name)

        # Start with any existing explicit mapping
        current = existing.get(lib_id, "")

        # Try to auto-match a host subfolder to this library
        matched_folder = ""
        match_reason   = ""
        for sf in host_subfolders:
            if sf.lower() == lib_name.lower():
                matched_folder = sf; match_reason = "exact"; break
            if slug(sf) == lib_slug:
                matched_folder = sf; match_reason = "normalised"; break
        if not matched_folder:
            for sf in host_subfolders:
                if lib_slug in slug(sf) or slug(sf) in lib_slug:
                    matched_folder = sf; match_reason = "partial"; break

        suggested_path = os.path.join(books_root, matched_folder) if (books_root and matched_folder) else ""

        # Check the current/suggested path right now
        check_path = current or suggested_path
        accessible, host_subfolders_check, host_error = _check_host_path(check_path)

        proposals.append({
            "id":             lib_id,
            "library":        lib_name,
            "kavita_folder":  kavita_folder,
            "current_path":   current,
            "suggested_path": suggested_path,
            "match_reason":   match_reason,
            "accessible":     accessible,
            "host_error":     host_error,
        })

    result = {
        "ok":             True,
        "books_root":     books_root,
        "host_subfolders": host_subfolders,
        "proposals":      proposals,
    }
    if scan_error:
        result["scan_error"] = scan_error
    return result


# ── Kavita ────────────────────────────────────────────────────────────────────

@router.get("/libraries")
async def get_libraries(current_user: str = Depends(require_auth)):
    libs = await kavita.fetch_libraries()
    for lib in libs:
        kavita_folder = (lib.get("folders") or [""])[0]
        lib["host_folder"] = resolve_library_path(kavita_folder, library_id=lib.get("id"))
        lib["kavita_folder"] = kavita_folder  # Store Kavita's internal path for scanning
    return libs


@router.get("/subjects/{library_path:path}")
async def get_subjects(
    library_path: str,
    current_user: str = Depends(require_auth),
):
    if not os.path.exists(library_path):
        return []
    try:
        return sorted(
            e.name for e in os.scandir(library_path) if e.is_dir()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Metadata lookup ───────────────────────────────────────────────────────────



@router.post("/metadata/batch")
async def get_metadata_batch(
    files: list[UploadFile] = File(...),
    current_user: str = Depends(require_auth),
):
    """
    Batch metadata lookup for multiple files in a single DeepSeek call.
    Much more efficient than individual lookups.
    """
    # Fetch libraries for DeepSeek selection
    libraries = []
    try:
        libs = await kavita.fetch_libraries()
        libraries = [{"id": lib.get("id"), "name": lib.get("name")} for lib in libs]
    except Exception as exc:
        logger.warning(f"Failed to fetch libraries for batch metadata: {exc}")
    
    # Extract metadata from all files
    queries = []
    file_data = []
    for file in files:
        data = await file.read()
        file_data.append(data)
        embedded = md.extract_from_file(data, file.filename)
        query = md.title_to_query(embedded["title"], file.filename, embedded.get("extracted_text", ""))
        queries.append({
            "filename": file.filename,
            "query": query,
            "author": embedded.get("author", ""),
            "extracted_text": embedded.get("extracted_text", "")
        })
    
    # Call LLM batch API
    llm_key = app_settings.get("llm_api_key", "").strip()
    llm_provider = app_settings.get("llm_provider", "deepseek")
    llm_model = app_settings.get("llm_model", "deepseek-chat")
    llm_base_url = app_settings.get("llm_base_url", "https://api.deepseek.com/v1")
    deepseek_results = []
    
    # Set base_url for different providers
    if llm_provider == "deepseek":
        llm_base_url = llm_base_url or "https://api.deepseek.com/v1"
    elif llm_provider == "openai":
        llm_base_url = None  # OpenAI uses default
    elif llm_provider == "anthropic":
        llm_base_url = None  # Anthropic has its own SDK
    
    # Call both LLM and Open Library in parallel
    import asyncio
    
    tasks = []
    if llm_key:
        tasks.append(md.search_llm_batch(queries, llm_key, libraries, base_url=llm_base_url, model=llm_model))
    
    enable_ol = app_settings.get("enable_open_library", True)
    if enable_ol:
        # Call Open Library for each query
        ol_tasks = [md.search_open_library(q["query"]) for q in queries]
        tasks.append(asyncio.gather(*ol_tasks, return_exceptions=True))
    
    results_list = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    
    deepseek_results = results_list[0] if llm_key and len(results_list) > 0 else []
    openlibrary_results = results_list[1] if enable_ol and len(results_list) > 1 else []
    
    # Map results back to files by index
    results = []
    for i, (query_info, data) in enumerate(zip(queries, file_data)):
        embedded = md.extract_from_file(data, query_info["filename"])
        
        # Find the corresponding DeepSeek result by index
        ds_result = None
        for r in deepseek_results:
            if r.get("index") == i:
                ds_result = r
                break
        
        # Get Open Library result for this query
        ol_result = openlibrary_results[i] if enable_ol and i < len(openlibrary_results) else []
        if isinstance(ol_result, Exception):
            ol_result = []
        
        result = {
            "filename": query_info["filename"],
            "embedded": embedded,
            "deepseek_result": ds_result or None,
            "openlibrary_result": ol_result,
        }
        results.append(result)
    
    return {"results": results}


@router.post("/metadata")
async def get_metadata(
    file: UploadFile = File(...),
    current_user: str = Depends(require_auth),
):
    data = await file.read()
    embedded = md.extract_from_file(data, file.filename)
    query = md.title_to_query(embedded["title"], file.filename, embedded.get("extracted_text", ""))
    
    # Fetch libraries for DeepSeek selection
    libraries = []
    try:
        libs = await kavita.fetch_libraries()
        libraries = [{"id": lib.get("id"), "name": lib.get("name")} for lib in libs]
    except Exception as exc:
        logger.warning(f"Failed to fetch libraries for metadata: {exc}")
    
    # Search both DeepSeek and Open Library in parallel
    deepseek_results = []
    openlibrary_results = []
    
    llm_key = app_settings.get("llm_api_key", "").strip()
    llm_provider = app_settings.get("llm_provider", "deepseek")
    llm_model = app_settings.get("llm_model", "deepseek-chat")
    llm_base_url = app_settings.get("llm_base_url", "https://api.deepseek.com/v1")
    
    # Set base_url for different providers
    if llm_provider == "deepseek":
        llm_base_url = llm_base_url or "https://api.deepseek.com/v1"
    elif llm_provider == "openai":
        llm_base_url = None  # OpenAI uses default
    elif llm_provider == "anthropic":
        llm_base_url = None  # Anthropic has its own SDK
    
    if llm_key:
        deepseek_results = await md.search_llm(query, llm_key, embedded.get("author", ""), libraries, base_url=llm_base_url, model=llm_model)
    
    if app_settings.get("enable_open_library", True):
        openlibrary_results = await md.search_open_library(query)
    
    # Prioritize preferred subjects in results
    preferred_subjects = set(app_settings.get("preferred_subjects", []))
    
    def prioritize_subjects(subjects):
        if not subjects:
            return []
        # Sort subjects: preferred ones first, then alphabetically
        return sorted(subjects, key=lambda s: (s not in preferred_subjects, s.lower()))
    
    for result in deepseek_results:
        if result.get("subjects"):
            result["subjects"] = prioritize_subjects(result["subjects"])
    
    for result in openlibrary_results:
        if result.get("subjects"):
            result["subjects"] = prioritize_subjects(result["subjects"])
    
    return {
        "embedded": embedded,
        "suggestions": openlibrary_results,
        "deepseek_results": deepseek_results,
        "query_used": query,
        "preferred_subjects": list(preferred_subjects),
    }


# ── Upload ────────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    library_id: int = Form(...),
    library_path: str = Form(...),
    subject: str = Form(...),
    title: str = Form(...),
    author: str = Form(default=""),
    year: str = Form(default=""),
    publisher: str = Form(default=""),
    current_user: str = Depends(require_auth),
):
    s_subject   = sanitize_name(subject)
    s_title     = sanitize_name(title)
    s_author    = sanitize_name(author)
    s_year      = sanitize_name(year)
    s_publisher = sanitize_name(publisher)
    ext         = os.path.splitext(file.filename)[1]
    host_path   = resolve_library_path(library_path, library_id=library_id)
    target      = os.path.join(host_path, s_subject, s_title, f"{s_title}{ext}")

    logger.info(f"Upload: {file.filename} -> {target} (kavita path: {library_path})")
    try:
        await _write_file(file, target)
    except Exception as exc:
        logger.error(f"Write failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    scan_ok = False
    if library_path:
        # Targeted scan: tell Kavita to look at just the folder we wrote to
        scan_ok = await kavita.scan_folder(library_path)
    if not scan_ok and library_id:
        # Fallback to full library scan if scan-folder wasn't accepted
        await kavita.scan_library(library_id)
        scan_ok = True

    return {"status": "success", "path": target, "kavita_synced": scan_ok}


@router.post("/bulk-upload")
async def bulk_upload(
    files: List[UploadFile] = File(...),
    library_ids:   str = Form(...),
    library_paths: str = Form(...),
    subjects:      str = Form(...),
    titles:        str = Form(...),
    authors:       str = Form(default="[]"),
    years:         str = Form(default="[]"),
    publishers:    str = Form(default="[]"),
    current_user: str = Depends(require_auth),
):
    lib_ids   = json.loads(library_ids)
    lib_paths = json.loads(library_paths)
    subjs     = json.loads(subjects)
    titls     = json.loads(titles)
    try:
        authors = json.loads(authors) if authors and authors != "[]" else [""] * len(files)
    except:
        authors = [""] * len(files)
    try:
        years = json.loads(years) if years and years != "[]" else [""] * len(files)
    except:
        years = [""] * len(files)
    try:
        publishers = json.loads(publishers) if publishers and publishers != "[]" else [""] * len(files)
    except:
        publishers = [""] * len(files)

    if not (len(files) == len(lib_ids) == len(lib_paths) == len(subjs) == len(titls) == len(authors) == len(years) == len(publishers)):
        raise HTTPException(status_code=400, detail="Array length mismatch")

    await kavita.get_token()

    results = []
    # Track the Kavita-side folder paths that had successful writes.
    # We use these for targeted scan-folder calls, deduped so we don't
    # fire duplicate scans when multiple files land in the same library.
    folders_to_scan: set[str] = set()   # Kavita-internal paths (e.g. /manga/Physics)
    libs_to_scan:    set[int] = set()   # fallback: library IDs

    for file, lib_id, lib_path, subject, title, author, year, publisher in zip(files, lib_ids, lib_paths, subjs, titls, authors, years, publishers):
        s_subject   = sanitize_name(subject)
        s_title     = sanitize_name(title)
        s_author    = sanitize_name(author)
        s_year      = sanitize_name(year)
        s_publisher = sanitize_name(publisher)
        ext         = os.path.splitext(file.filename)[1]
        host_path   = resolve_library_path(lib_path, library_id=lib_id)
        target      = os.path.join(host_path, s_subject, s_title, f"{s_title}{ext}")

        try:
            await _write_file(file, target)
            results.append({"file": file.filename, "status": "ok", "path": target})
            if lib_path:
                folders_to_scan.add(lib_path)
            elif lib_id:
                libs_to_scan.add(lib_id)
        except Exception as exc:
            logger.error(f"Bulk upload failed for {file.filename}: {exc}")
            results.append({"file": file.filename, "status": "error", "error": str(exc)})

    # Trigger targeted scans first; fall back to full library scan if rejected
    scanned_lib_ids: set[int] = set()
    logger.info(f"[Bulk Upload] folders_to_scan = {folders_to_scan}, libs_to_scan = {libs_to_scan}")
    logger.info(f"[Bulk Upload] Total files uploaded: {len(results)}")
    
    if not folders_to_scan and not libs_to_scan:
        logger.warning("Bulk upload: No folders or libraries to scan - library_path might be empty!")
    
    for folder in folders_to_scan:
        logger.info(f"[Bulk Upload] Scanning folder: {folder}")
        ok = await kavita.scan_folder(folder)
        logger.info(f"[Bulk Upload] scan_folder({folder}) result: {ok}")
        if not ok:
            # Find the library ID for this folder and queue a full scan
            lib_id = next(
                (lid for lid, lp in zip(lib_ids, lib_paths) if lp == folder),
                None,
            )
            if lib_id:
                libs_to_scan.add(lib_id)

    for lib_id in libs_to_scan:
        if lib_id not in scanned_lib_ids:
            logger.info(f"Bulk upload: scanning library {lib_id}")
            await kavita.scan_library(lib_id)
            scanned_lib_ids.add(lib_id)

    ok  = sum(1 for r in results if r["status"] == "ok")
    err = len(results) - ok
    return {"ok": ok, "errors": err, "results": results}
