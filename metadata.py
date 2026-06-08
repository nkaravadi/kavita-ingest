"""
Book metadata: extract from file bytes and look up via Open Library.
"""
import io
import os
import re
import zipfile
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from openai import AsyncOpenAI

from config import get_logger

logger = get_logger(__name__)


def _clean(value) -> str:
    """Strip whitespace and null bytes from a metadata string."""
    return str(value).strip().strip("\x00") if value else ""


def extract_text_from_pages(data: bytes, filename: str, max_pages: int = 3) -> str:
    """
    Extract text from the first few pages of a PDF or EPUB for better title/author detection.
    Returns concatenated text from the first max_pages pages.
    """
    ext = Path(filename).suffix.lower()
    text_parts = []
    
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            for i in range(min(len(reader.pages), max_pages)):
                page = reader.pages[i]
                text_parts.append(page.extract_text() or "")
        
        elif ext == ".epub":
            from ebooklib import epub
            with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tf:
                tf.write(data)
                tf_path = tf.name
            try:
                book = epub.read_epub(tf_path)
                # Get first few chapters
                for item in list(book.get_items())[:max_pages]:
                    if isinstance(item, epub.EpubHtml):
                        text_parts.append(item.get_body_content().decode('utf-8', errors='ignore') or "")
            finally:
                os.unlink(tf_path)
    
    except Exception as exc:
        logger.warning(f"Failed to extract text from {filename}: {exc}")
    
    # Clean up the extracted text
    full_text = " ".join(text_parts)
    # Remove excess whitespace
    full_text = re.sub(r'\s+', ' ', full_text)
    # Limit length - more text for better library matching
    full_text = full_text[:8000]
    return full_text


def extract_from_file(data: bytes, filename: str) -> dict:
    """
    Extract embedded title / author / subject from PDF, EPUB, or CBZ/CBR bytes.
    Returns a dict with keys ``title``, ``author``, ``subject``, ``extracted_text`` (all may be empty).
    """
    ext = Path(filename).suffix.lower()
    result = {"title": "", "author": "", "subject": "", "extracted_text": ""}

    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            meta = PdfReader(io.BytesIO(data)).metadata or {}
            result["title"]   = _clean(meta.get("/Title",   meta.get("title",   "")))
            result["author"]  = _clean(meta.get("/Author",  meta.get("author",  "")))
            result["subject"] = _clean(meta.get("/Subject", meta.get("subject", "")))

        elif ext == ".epub":
            from ebooklib import epub
            with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tf:
                tf.write(data)
                tf_path = tf.name
            try:
                book = epub.read_epub(tf_path)
                titles   = book.get_metadata("DC", "title")
                authors  = book.get_metadata("DC", "creator")
                subjects = book.get_metadata("DC", "subject")
                result["title"]   = _clean(titles[0][0])   if titles   else ""
                result["author"]  = _clean(authors[0][0])  if authors  else ""
                result["subject"] = _clean(subjects[0][0]) if subjects else ""
            finally:
                os.unlink(tf_path)

        elif ext in (".cbz", ".cbr"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                comic_info = next(
                    (n for n in zf.namelist() if n.lower() == "comicinfo.xml"), None
                )
                if comic_info:
                    root = ET.fromstring(zf.read(comic_info))
                    result["title"]   = _clean(root.findtext("Title")  or "")
                    result["author"]  = _clean(root.findtext("Writer") or "")
                    result["subject"] = _clean(root.findtext("Genre")  or "")

    except Exception as exc:
        logger.warning(f"Metadata extraction failed for {filename}: {exc}")

    # Also extract text from first few pages for better search
    result["extracted_text"] = extract_text_from_pages(data, filename)

    return result


def title_to_query(embedded_title: str, filename: str, extracted_text: str = "") -> str:
    """
    Build a clean search query from embedded title, filename, and extracted text.
    Prioritizes embedded title, then falls back to extracted text, then filename.
    """
    raw = embedded_title or extracted_text or Path(filename).stem
    # Replace separators with spaces
    query = re.sub(r"[_.]", " ", raw)
    # Remove trailing 4-digit years
    query = re.sub(r"\s+\d{4}\s*$", "", query).strip()
    # Remove common noise words at the end
    query = re.sub(r"\s+(Final|Revised|Edition|Ed\.|v\d+(\.\d+)?)\s*$", "", query, flags=re.IGNORECASE).strip()
    # Remove years that appear in the middle (e.g. "2024")
    query = re.sub(r"\s+\d{4}\s+", " ", query).strip()
    # Try to remove author name pattern at start: "Author Name Title" -> "Title"
    # Author names are typically 1-3 words followed by the actual title
    words = query.split()
    if len(words) > 3:
        # Check if first word looks like an author (ends with initial like "A" or "J.")
        if re.match(r"^[A-Z][a-z]+ [A-Z]\.?$", " ".join(words[:2])):
            query = " ".join(words[2:])
    return query




async def search_llm_batch(queries: list, api_key: str, libraries: list = None, base_url: str = None, model: str = "deepseek-chat") -> list:
    """
    Search LLM API for multiple book metadata requests in a single call.
    queries: list of dict with 'filename' and 'query' keys
    libraries: list of dict with 'id' and 'name' keys for library selection
    base_url: custom API base URL (for DeepSeek or other OpenAI-compatible APIs)
    model: model name to use
    Returns list of results corresponding to each query.
    """
    if not queries or not api_key.strip():
        return []
    try:
        # Configure OpenAI client
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        
        client = AsyncOpenAI(**client_kwargs)
        
        # Add library info to prompt if available
        library_info = ""
        if libraries:
            lib_names = [f"{lib['name']} (ID: {lib['id']})" for lib in libraries]
            library_info = f"""
Available libraries to choose from:
{chr(10).join(f"- {name}" for name in lib_names)}

For each book, select the most appropriate library from the list based on subject matter. Return the library ID in "suggested_library_id". Return null if none match well."""
        
        # Build batch prompt - include extracted text for better library matching
        query_list = ""
        for i, q in enumerate(queries):
            query_list += f"{i+1}. File: {q['filename']}\n"
            query_list += f"   Query: {q['query']}\n"
            if q.get('extracted_text'):
                query_list += f"   Content preview: {q['extracted_text'][:1000]}...\n"
            query_list += "\n"
        
        logger.info(f"[LLM Batch] Sending {len(queries)} queries to LLM")
        
        prompt = f"""You are a book metadata assistant. Given a list of book search queries, return a JSON array of results with the following structure:
[
  {{
    "index": 0,
    "title": "exact book title",
    "author": "primary author name",
    "year": publication_year_as_number,
    "publisher": "publisher name",
    "subjects": ["subject1", "subject2", "subject3"],
    "suggested_library_id": library_id_or_null,
    "match_confidence": 0.95
  }}
]
{library_info}

Search queries and content previews:
{query_list}

Instructions:
1. For each query, find the best matching book
2. Extract accurate metadata: title, author, year, publisher, subjects
3. Select the most appropriate library from the list based on subject matter - use the content preview to determine the actual topic
4. Assign a match_confidence score (0.0 to 1.0) indicating how well the result matches the query
5. Use the "index" field to match results to the input queries (0-based)
6. Return results in the same order as the input queries

Return ONLY valid JSON, no markdown, no other text. If no good matches are found for a query, return the closest matches with lower confidence scores."""
        
        logger.info(f"[LLM Batch] Prompt being sent to LLM (first 1500 chars):\n{prompt[:1500]}...")
        
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a book metadata assistant. Always return valid JSON arrays with the exact structure specified. No markdown, no explanations, just JSON."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
        )
        
        content = response.choices[0].message.content
        logger.info(f"[LLM Batch] Full response: {content}")
        
        # Try to extract JSON from the response
        import json
        import re
        try:
            # Find JSON array in the response (handle markdown code blocks)
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                results = json.loads(json_match.group())
                if isinstance(results, list):
                    # Ensure results are in the correct order by index
                    sorted_results = sorted(results, key=lambda x: x.get('index', 0))
                    return sorted_results
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM batch JSON response: {e}")
        
        return []
    except Exception as exc:
        logger.warning(f"LLM batch lookup failed: {exc}", exc_info=True)
        return []


async def search_deepseek(query: str, api_key: str, author: str = "", libraries: list = None) -> list:
    """Backwards compatibility wrapper for search_llm."""
    return await search_llm(query, api_key, author, libraries, base_url="https://api.deepseek.com/v1", model="deepseek-chat")
    """
    Search DeepSeek API for book information. Returns up to 5 candidates.
    DeepSeek uses OpenAI-compatible API format.
    """
    if not query.strip() or not api_key.strip():
        return []
    try:
        # Build a more specific prompt
        author_hint = f" by {author}" if author else ""
        
        # Add library info to prompt if available
        library_info = ""
        if libraries:
            lib_names = [f"{lib['name']} (ID: {lib['id']})" for lib in libraries]
            library_info = f"""
Available libraries to choose from:
{chr(10).join(f"- {name}" for name in lib_names)}

For the best matching book, analyze the subject matter and select the most appropriate library from the list above. Return the library ID in "suggested_library_id". Return null if none match well."""
        
        prompt = f"""You are a book metadata assistant. Given a book search query, return a JSON array of up to 5 matching books with the following structure:
[
  {{
    "title": "exact book title",
    "author": "primary author name",
    "year": publication_year_as_number,
    "publisher": "publisher name",
    "subjects": ["subject1", "subject2", "subject3"],
    "suggested_library_id": library_id_or_null,
    "match_confidence": 0.95
  }}
]
{library_info}
Search query: "{query}"{author_hint}

Instructions:
1. Find the best matching books based on title and subject
2. Extract accurate metadata: title, author, year, publisher, subjects
3. Select the most appropriate library from the list based on subject matter
4. Assign a match_confidence score (0.0 to 1.0) indicating how well the result matches the query
5. Sort results by match_confidence in descending order

Return ONLY valid JSON, no markdown, no other text. If no good matches are found, return the closest matches with lower confidence scores."""
        
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a book metadata assistant. Always return valid JSON arrays with the exact structure specified. No markdown, no explanations, just JSON."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    "temperature": 0.2,
                },
            )
        if resp.status_code != 200:
            logger.warning(f"DeepSeek API error {resp.status_code}: {resp.text[:200]}")
            return []
        
        content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.info(f"DeepSeek response: {content[:500]}")
        
        # Try to extract JSON from the response
        import json
        try:
            # Find JSON array in the response (handle markdown code blocks)
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                results = json.loads(json_match.group())
                # Validate structure
                validated = []
                for r in results[:5]:
                    if isinstance(r, dict) and "title" in r:
                        validated.append({
                            "title": r.get("title", ""),
                            "author": r.get("author", ""),
                            "year": r.get("year"),
                            "publisher": r.get("publisher", ""),
                            "subjects": r.get("subjects") if isinstance(r.get("subjects"), list) else []
                        })
                return validated
        except Exception as e:
            logger.warning(f"Failed to parse DeepSeek JSON response: {e}")
        return []
    except Exception as exc:
        logger.warning(f"DeepSeek lookup failed: {exc}", exc_info=True)
        return []


async def search_open_library(query: str) -> list:
    """
    Search Open Library by title. Returns up to 5 candidates, each with:
    ``title``, ``author``, ``subjects``, ``cover_url``, ``first_published``.
    """
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://openlibrary.org/search.json",
                params={
                    "title": query,
                    "limit": 5,
                    "fields": "title,author_name,subject,cover_i,first_publish_year",
                },
            )
        if resp.status_code != 200:
            logger.warning(f"Open Library returned status {resp.status_code}")
            return []
        results = []
        for doc in resp.json().get("docs", []):
            cover_id = doc.get("cover_i")
            results.append({
                "title": doc.get("title", ""),
                "author": (doc.get("author_name") or [""])[0],
                "subjects": (doc.get("subject") or [])[:6],
                "cover_url": (
                    f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
                    if cover_id else None
                ),
                "first_published": doc.get("first_publish_year"),
            })
        return results
    except Exception as exc:
        logger.warning(f"Open Library lookup failed: {exc}", exc_info=True)
        return []
