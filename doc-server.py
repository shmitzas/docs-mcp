#!/usr/bin/env python3
"""
CS2 Docs MCP Server

This MCP server provides documentation search capabilities for CS2-related
documentation (SwiftlyS2, the Source 2 Wiki, Swiftly-Tracker/CS2-Dumps, and
friends) stored in the docs/ directory. It allows GitHub Copilot and other
MCP clients to search and retrieve documentation content.
"""

import asyncio
import os
import re
import gc
import psutil
import time
import hashlib
import json
import sys
import threading
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from functools import lru_cache, wraps
from fastmcp import FastMCP

# Create the FastMCP server
mcp = FastMCP(
    name="CS2 Docs MCP",
    instructions=(
        "Use this server to search and retrieve CS2-related documentation "
        "(SwiftlyS2, the Source 2 Wiki, Swiftly-Tracker/CS2-Dumps, plus supporting projects). "
        "IMPORTANT USAGE RULES:\n"
        "1. NEVER guess document paths. Always obtain paths from browse_category or search_documentation results.\n"
        "2. DISCOVERY WORKFLOW: Call list_documentation_categories to see available categories, "
        "then call browse_category with the exact category name (e.g. 'swiftlys2') to list all documents "
        "and their exact paths. Use those paths with get_document.\n"
        "3. SEARCH QUERIES: search_documentation uses exact substring matching — use a single short keyword "
        "or a type/class name (e.g. 'ICommandContext', 'database', 'commands'). "
        "Do NOT use long natural-language phrases or multiple words joined together — they will return no results.\n"
        "4. PATH FORMAT: doc_path must be the relative path exactly as returned by browse_category or "
        "search_documentation results (e.g. 'swiftlys2/docs-api-commands-icommandcontext.md'). "
        "Do not construct or infer paths yourself."
    )
)

# Base documentation directory. Overridable via DOCS_ROOT env var — under
# Pterodactyl the persistent volume mounts at /home/container so we point
# DOCS_ROOT there instead of the (ephemeral) directory next to the script.
DOCS_DIR = Path(os.getenv('DOCS_ROOT', str(Path(__file__).parent / "docs")))

# Configuration for resource management (can be overridden via environment variables)
MAX_CONCURRENT_REQUESTS = int(os.getenv('MAX_CONCURRENT_REQUESTS', 100))  # Limit concurrent requests
FILE_CACHE_SIZE = int(os.getenv('FILE_CACHE_SIZE', 500))  # LRU cache size for file contents
CLEANUP_INTERVAL = int(os.getenv('CLEANUP_INTERVAL', 300))  # Run cleanup every 5 minutes
RESULT_CACHE_TTL = int(os.getenv('RESULT_CACHE_TTL', 60))  # Cache results for 60 seconds

# Global semaphore to limit concurrent requests
request_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# Request deduplication: track in-flight requests to avoid duplicate work
inflight_requests: Dict[str, asyncio.Future] = {}
inflight_lock = asyncio.Lock()

class TTLCache:
    """Simple TTL cache for function results"""
    
    def __init__(self, ttl: int = 60):
        self.cache: Dict[str, tuple[Any, float]] = {}
        self.ttl = ttl
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value: Any):
        """Set value in cache with current timestamp"""
        self.cache[key] = (value, time.time())
    
    def clear(self):
        """Clear all cached values"""
        self.cache.clear()
    
    def size(self) -> int:
        """Get current cache size"""
        # Clean expired entries first
        current_time = time.time()
        expired_keys = [k for k, (_, ts) in self.cache.items() if current_time - ts >= self.ttl]
        for k in expired_keys:
            del self.cache[k]
        return len(self.cache)

# Result cache for frequently called operations
result_cache = TTLCache(ttl=RESULT_CACHE_TTL)

class ResourceMonitor:
    """Monitor and log resource usage"""
    
    @staticmethod
    def get_open_files_count():
        """Get count of open file descriptors"""
        try:
            process = psutil.Process()
            return process.num_fds() if hasattr(process, 'num_fds') else len(process.open_files())
        except:
            return -1
    
    @staticmethod
    def log_resources():
        """Log current resource usage"""
        try:
            process = psutil.Process()
            open_files = ResourceMonitor.get_open_files_count()
            memory_info = process.memory_info()
            print(f"Resource usage - Open FDs: {open_files}, Memory: {memory_info.rss / 1024 / 1024:.2f} MB")
        except Exception as e:
            print(f"Error monitoring resources: {e}")

def cached_and_deduplicated(cache_key_func=None):
    """
    Decorator that adds both caching and request deduplication.
    
    - Caching: Stores results for RESULT_CACHE_TTL seconds
    - Deduplication: Multiple concurrent identical requests share the same execution
    
    Args:
        cache_key_func: Optional function to generate cache key from args. 
                       If None, uses json.dumps of kwargs.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(**kwargs):
            # Generate cache key
            if cache_key_func:
                cache_key = cache_key_func(**kwargs)
            else:
                # Default: use function name + sorted kwargs as key
                cache_key = f"{func.__name__}:{json.dumps(kwargs, sort_keys=True)}"
            
            # Check result cache first
            cached_result = result_cache.get(cache_key)
            if cached_result is not None:
                return cached_result
            
            # Check if request is already in-flight
            async with inflight_lock:
                if cache_key in inflight_requests:
                    # Wait for the in-flight request to complete
                    future = inflight_requests[cache_key]
                else:
                    # Create new future for this request
                    future = asyncio.Future()
                    inflight_requests[cache_key] = future
            
            # If we're waiting on another request, await it
            if future.done() or cache_key not in inflight_requests or inflight_requests[cache_key] != future:
                try:
                    result = await future
                    return result
                except Exception:
                    # If the other request failed, try ourselves
                    pass
            
            # We're the first request, execute the function
            try:
                result = await func(**kwargs)
                
                # Cache the result
                result_cache.set(cache_key, result)
                
                # Set the future result for any waiting requests
                if not future.done():
                    future.set_result(result)
                
                return result
            except Exception as e:
                # Propagate exception to waiting requests
                if not future.done():
                    future.set_exception(e)
                raise
            finally:
                # Remove from in-flight requests
                async with inflight_lock:
                    inflight_requests.pop(cache_key, None)
        
        return wrapper
    return decorator

class DocSearcher:
    """Documentation search and retrieval tool"""
    
    def __init__(self, docs_path: Path):
        self.docs_path = docs_path
        self.doc_index = {}  # Only store metadata, not full content
        self._index_documents()
    
    def _index_documents(self):
        """Index all text-based files in the docs directory (metadata only)"""
        if not self.docs_path.exists():
            print(f"Warning: Documentation directory not found: {self.docs_path}")
            return
        
        for doc_file in self.docs_path.rglob("*"):
            if not doc_file.is_file():
                continue
            try:
                with open(doc_file, 'r', encoding='utf-8') as f:
                    # Only read first few lines to get title, not full content
                    first_lines = [next(f, '') for _ in range(10)]
                    title = self._extract_title(''.join(first_lines), doc_file.name)
                    
                    relative_path = doc_file.relative_to(self.docs_path)
                    self.doc_index[str(relative_path)] = {
                        'path': str(relative_path),
                        'full_path': str(doc_file),
                        'title': title
                    }
            except (UnicodeDecodeError, ValueError):
                # Skip binary files that can't be read as text
                pass
            except Exception as e:
                print(f"Error indexing {doc_file}: {e}")
    
    def _extract_title(self, content: str, filename: str = "") -> str:
        """Extract title from document content, falling back to filename"""
        lines = content.split('\n')
        for line in lines[:10]:  # Check first 10 lines
            if line.startswith('# '):
                return line[2:].strip()
        # For non-markdown files, use the filename (without extension) as the title
        if filename:
            return Path(filename).stem
        return "Untitled"
    
    @lru_cache(maxsize=FILE_CACHE_SIZE)
    def _load_content(self, doc_path: str) -> str:
        """Lazy load document content on demand with LRU caching"""
        doc_info = self.doc_index.get(doc_path)
        if not doc_info:
            return ""
        
        try:
            # Use context manager to ensure file is always closed
            with open(doc_info['full_path'], 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            print(f"Error loading {doc_path}: {e}")
            return ""
    
    def clear_cache(self):
        """Clear the file content cache to free memory"""
        self._load_content.cache_clear()
        gc.collect()  # Force garbage collection

    def reindex(self) -> int:
        """
        Rebuild the metadata index from disk and clear all content caches.
        Safe to call from a background thread while requests are in flight:
        we build the new index into a local dict, swap it in atomically
        (single reference assignment), then drop cached file contents.
        Returns the new document count.
        """
        old_docs_path = self.docs_path
        # Re-honour DOCS_ROOT in case it changed (rare but cheap).
        self.docs_path = Path(os.getenv('DOCS_ROOT', str(old_docs_path)))
        new_index: Dict[str, Dict[str, str]] = {}
        if self.docs_path.exists():
            for doc_file in self.docs_path.rglob("*"):
                if not doc_file.is_file():
                    continue
                try:
                    with open(doc_file, 'r', encoding='utf-8') as f:
                        first_lines = [next(f, '') for _ in range(10)]
                        title = self._extract_title(''.join(first_lines), doc_file.name)
                        relative_path = doc_file.relative_to(self.docs_path)
                        new_index[str(relative_path)] = {
                            'path': str(relative_path),
                            'full_path': str(doc_file),
                            'title': title
                        }
                except (UnicodeDecodeError, ValueError):
                    pass
                except Exception as e:
                    print(f"Error indexing {doc_file}: {e}")
        else:
            print(f"Warning: Documentation directory not found: {self.docs_path}")
        # Atomic swap — readers holding the old dict finish their loop safely.
        self.doc_index = new_index
        # Drop cached file contents so freshly-edited files aren't served stale.
        self.clear_cache()
        # Also drop the tool-level TTL result cache: otherwise MCP clients
        # calling the same query within RESULT_CACHE_TTL seconds after a
        # reindex would get the pre-update JSON. dict.clear() is GIL-atomic
        # so this is safe from a non-asyncio thread.
        result_cache.clear()
        return len(new_index)
    
    def search_docs(self, query: str, max_results: int = 10) -> List[Dict[str, Any]]:
        """Search documentation by query string"""
        query_lower = query.lower()
        results = []
        
        for doc_path, doc_info in self.doc_index.items():
            title_lower = doc_info['title'].lower()
            
            # First check title for quick filtering
            score = 0
            if query_lower in title_lower:
                score += 10
                # Only load content for title matches or if needed
                content = self._load_content(doc_path)
                content_lower = content.lower()
                if query_lower in content_lower:
                    score += content_lower.count(query_lower)
                context = self._extract_context(content, query, 300)
            else:
                # Check content for non-title matches
                content = self._load_content(doc_path)
                content_lower = content.lower()
                if query_lower in content_lower:
                    score += content_lower.count(query_lower)
                    context = self._extract_context(content, query, 300)
                else:
                    continue
            
            if score > 0:
                results.append({
                    'path': doc_path,
                    'title': doc_info['title'],
                    'score': score,
                    'context': context
                })
        
        # Sort by relevance and return top results
        results.sort(key=lambda x: x['score'], reverse=True)
        return results[:max_results]
    
    def _extract_context(self, content: str, query: str, context_length: int = 300) -> str:
        """Extract context around the query match"""
        query_lower = query.lower()
        content_lower = content.lower()
        
        pos = content_lower.find(query_lower)
        if pos == -1:
            return content[:context_length] + "..." if len(content) > context_length else content
        
        start = max(0, pos - context_length // 2)
        end = min(len(content), pos + len(query) + context_length // 2)
        
        context = content[start:end]
        if start > 0:
            context = "..." + context
        if end < len(content):
            context = context + "..."
        
        return context
    
    def get_doc_content(self, doc_path: str) -> Dict[str, Any]:
        """Get full content of a specific document"""
        if doc_path in self.doc_index:
            content = self._load_content(doc_path)
            return {
                'path': doc_path,
                'title': self.doc_index[doc_path]['title'],
                'content': content
            }
        return {'error': f'Document not found: {doc_path}'}
    
    def list_categories(self) -> List[str]:
        """List all top-level documentation categories (subdirectories of docs/)"""
        categories = set()
        for doc_path in self.doc_index.keys():
            parts = Path(doc_path).parts
            if len(parts) > 0:
                categories.add(parts[0])
        return sorted(list(categories))
    
    def get_docs_by_category(self, category: str) -> List[Dict[str, Any]]:
        """Get all documents whose top-level category exactly matches the given name"""
        results = []
        for doc_path, doc_info in self.doc_index.items():
            parts = Path(doc_path).parts
            if parts and parts[0].lower() == category.lower():
                results.append({
                    'path': doc_path,
                    'title': doc_info['title']
                })
        return results

    def get_category_tree(self) -> Dict[str, Any]:
        """
        Build a hierarchical tree of categories, sub-categories, and documents.

        Structure:
          {
            "<category>": {
              "documents": [{"path": ..., "title": ...}, ...],
              "subcategories": {
                "<subcategory>": {
                  "documents": [{"path": ..., "title": ...}, ...]
                }, ...
              }
            }, ...
          }
        """
        tree: Dict[str, Any] = {}
        for doc_path, doc_info in self.doc_index.items():
            parts = Path(doc_path).parts
            entry = {'path': doc_path, 'title': doc_info['title']}
            if len(parts) == 1:
                # Document directly in docs/ root (unusual)
                cat = '__root__'
                tree.setdefault(cat, {'documents': [], 'subcategories': {}})
                tree[cat]['documents'].append(entry)
            elif len(parts) == 2:
                # docs/<category>/document
                cat = parts[0]
                tree.setdefault(cat, {'documents': [], 'subcategories': {}})
                tree[cat]['documents'].append(entry)
            else:
                # docs/<category>/<subcategory>/...document
                cat = parts[0]
                subcat = parts[1]
                tree.setdefault(cat, {'documents': [], 'subcategories': {}})
                tree[cat]['subcategories'].setdefault(subcat, {'documents': []})
                tree[cat]['subcategories'][subcat]['documents'].append(entry)
        return tree

# Initialize the documentation searcher
doc_searcher = DocSearcher(DOCS_DIR)

@mcp.tool()
@cached_and_deduplicated()
async def search_documentation(query: str, max_results: int = 10) -> dict:
    """
    Search CS2 documentation for relevant content using exact substring matching.

    IMPORTANT: This tool matches documents where the query string appears as an exact substring
    in the title or content. Use a SINGLE short keyword or a type/class name for best results.
    Examples of good queries: "ICommandContext", "database", "commands", "IConVar"
    Examples of BAD queries: "command context interface properties sender reply args"
    (multi-word natural-language phrases will almost always return zero results)

    To discover what documents exist, prefer browse_category over search_documentation.
    Use the exact 'path' values from results when calling get_document.
    
    Args:
        query: A single keyword or type/class name to search for (e.g., "ICommandContext", "database")
        max_results: Maximum number of results to return (default: 10)
    
    Returns:
        A dictionary containing search results with paths, titles, scores, and context snippets
    """
    async with request_semaphore:
        results = doc_searcher.search_docs(query, max_results)
        return {
            "query": query,
            "total_results": len(results),
            "results": results
        }

@mcp.tool()
@cached_and_deduplicated()
async def get_document(doc_path: str) -> dict:
    """
    Retrieve the full content of a specific documentation file.

    IMPORTANT: doc_path MUST be a path exactly as returned by browse_category or
    search_documentation (e.g. "swiftlys2/docs-api-commands-icommandcontext.md").
    Never construct or guess a path — always use a value from browse/search results.
    
    Args:
        doc_path: Exact relative path to the documentation file as returned by browse_category
                  or search_documentation (e.g. "swiftlys2/docs-api-commands-icommandcontext.md")
    
    Returns:
        A dictionary containing the document path, title, and full content
    """
    async with request_semaphore:
        return doc_searcher.get_doc_content(doc_path)

@mcp.tool()
@cached_and_deduplicated()
async def list_documentation_categories() -> dict:
    """
    List all available documentation categories with their sub-categories and document counts.
    Each top-level directory in docs/ is a category. Sub-directories within a category are
    sub-categories. Use browse_category to list the actual documents inside a category.
    
    Returns:
        A dictionary containing the full category tree with document counts
    """
    async with request_semaphore:
        tree = doc_searcher.get_category_tree()
        summary = {}
        for cat, data in tree.items():
            subcats = {}
            for subcat, subdata in data['subcategories'].items():
                subcats[subcat] = {'document_count': len(subdata['documents'])}
            summary[cat] = {
                'direct_document_count': len(data['documents']),
                'subcategories': subcats
            }
        return {
            "total_categories": len(summary),
            "categories": summary
        }

@mcp.tool()
@cached_and_deduplicated()
async def browse_category(category: str) -> dict:
    """
    Browse all documentation in a specific category, organized by sub-category.
    The category must be the exact name of a top-level directory inside docs/.
    Use list_documentation_categories first to discover valid category names and
    their sub-categories.
    
    Args:
        category: Exact category name (top-level directory in docs/, e.g. "swiftlys2")
    
    Returns:
        A dictionary with direct documents and sub-categories (each with their documents)
    """
    async with request_semaphore:
        tree = doc_searcher.get_category_tree()
        cat_lower = category.lower()
        # Find matching category (case-insensitive)
        matched_key = next((k for k in tree if k.lower() == cat_lower), None)
        if matched_key is None:
            available = sorted(tree.keys())
            return {
                "error": f"Category '{category}' not found.",
                "available_categories": available
            }
        data = tree[matched_key]
        return {
            "category": matched_key,
            "direct_documents": data['documents'],
            "subcategories": {
                subcat: subdata['documents']
                for subcat, subdata in data['subcategories'].items()
            },
            "total_documents": len(data['documents']) + sum(
                len(sd['documents']) for sd in data['subcategories'].values()
            )
        }

@mcp.tool()
@cached_and_deduplicated()
async def get_api_overview() -> dict:
    """
    Get an overview of the CS2 documentation.
    
    Returns:
        A dictionary containing statistics and overview of available documentation
    """
    async with request_semaphore:
        total_docs = len(doc_searcher.doc_index)
        tree = doc_searcher.get_category_tree()
        categories_summary = {}
        for cat, data in tree.items():
            subcats = list(data['subcategories'].keys())
            categories_summary[cat] = {
                'direct_document_count': len(data['documents']),
                'subcategories': subcats
            }
        return {
            "total_documents": total_docs,
            "total_categories": len(categories_summary),
            "categories": categories_summary
        }

@mcp.tool()
async def get_server_health() -> dict:
    """
    Get server health and resource usage statistics.
    
    Returns:
        A dictionary containing server health metrics, resource usage, and cache statistics
    """
    try:
        process = psutil.Process()
        open_files = ResourceMonitor.get_open_files_count()
        memory_info = process.memory_info()
        cache_info = doc_searcher._load_content.cache_info()
        
        # Calculate semaphore availability
        available_slots = request_semaphore._value
        
        # Get result cache stats
        result_cache_size = result_cache.size()
        
        # Get inflight request count
        async with inflight_lock:
            inflight_count = len(inflight_requests)
        
        return {
            "status": "healthy",
            "resources": {
                "open_file_descriptors": open_files,
                "memory_mb": round(memory_info.rss / 1024 / 1024, 2),
                "memory_percent": round(process.memory_percent(), 2),
                "cpu_percent": round(process.cpu_percent(), 2)
            },
            "file_cache": {
                "hits": cache_info.hits,
                "misses": cache_info.misses,
                "current_size": cache_info.currsize,
                "max_size": FILE_CACHE_SIZE,
                "hit_rate": round(cache_info.hits / (cache_info.hits + cache_info.misses) * 100, 2) if (cache_info.hits + cache_info.misses) > 0 else 0
            },
            "result_cache": {
                "current_size": result_cache_size,
                "ttl_seconds": RESULT_CACHE_TTL
            },
            "concurrency": {
                "max_concurrent_requests": MAX_CONCURRENT_REQUESTS,
                "available_slots": available_slots,
                "active_requests": MAX_CONCURRENT_REQUESTS - available_slots,
                "deduplicated_requests": inflight_count
            },
            "indexed_documents": len(doc_searcher.doc_index)
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@mcp.tool()
async def clear_cache() -> dict:
    """
    Manually clear the file content cache and run garbage collection.
    Useful for freeing memory when needed.
    
    Returns:
        A dictionary with before/after memory statistics
    """
    try:
        # Get stats before cleanup
        process = psutil.Process()
        memory_before = process.memory_info().rss / 1024 / 1024
        cache_info_before = doc_searcher._load_content.cache_info()
        result_cache_size_before = result_cache.size()
        
        # Perform cleanup
        doc_searcher.clear_cache()
        result_cache.clear()
        
        # Clear inflight requests (shouldn't be any, but just in case)
        async with inflight_lock:
            inflight_requests.clear()
        
        # Get stats after cleanup
        memory_after = process.memory_info().rss / 1024 / 1024
        cache_info_after = doc_searcher._load_content.cache_info()
        result_cache_size_after = result_cache.size()
        
        return {
            "status": "success",
            "memory_freed_mb": round(memory_before - memory_after, 2),
            "file_cache_before": {
                "size": cache_info_before.currsize,
                "hits": cache_info_before.hits,
                "misses": cache_info_before.misses
            },
            "file_cache_after": {
                "size": cache_info_after.currsize,
                "hits": cache_info_after.hits,
                "misses": cache_info_after.misses
            },
            "result_cache_before": {
                "size": result_cache_size_before
            },
            "result_cache_after": {
                "size": result_cache_size_after
            }
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


# ---------------------------------------------------------------------------
# Console command handler
# ---------------------------------------------------------------------------
# Pterodactyl schedules trigger tasks by writing a "console command" to the
# container's stdin. We read stdin from a daemon thread and dispatch the
# supported commands. The two useful ones:
#   update-docs  — run /app/update-all.sh (or $UPDATE_SCRIPT), then reindex
#   reindex      — rebuild the metadata index only (no fetch)
#
# The daily 04:00 schedule sends `update-docs`. Reindex happens inline once
# the update script exits, so no container restart is required to pick up
# new / renamed / deleted markdown files.
_update_lock = threading.Lock()


def _run_updates_and_reindex():
    """Shell out to the updater then rebuild the index. Serialised by lock."""
    if not _update_lock.acquire(blocking=False):
        print("[console] update-docs: another update is already running; ignoring")
        return
    try:
        script = os.getenv('UPDATE_SCRIPT', '/app/update-all.sh')
        if not os.path.isfile(script):
            print(f"[console] update-docs: script not found at {script}")
            return
        print(f"[console] update-docs: running {script}")
        # inherit stdout/stderr so Pterodactyl's console shows script output live
        rc = subprocess.call(['bash', script])
        print(f"[console] update-docs: script exited with rc={rc}; reindexing...")
        count = doc_searcher.reindex()
        print(f"[console] update-docs: reindex complete ({count} documents)")
    except Exception as e:
        print(f"[console] update-docs: error: {e}")
    finally:
        _update_lock.release()


def _console_reader():
    """Read stdin line-by-line and dispatch supported commands.

    Runs as a daemon thread — dies when the main process exits.
    Silently returns on EOF (e.g. detached stdin under `docker run -d`)."""
    try:
        for raw in sys.stdin:
            cmd = raw.strip().lower()
            if not cmd:
                continue
            if cmd == 'update-docs':
                # spawn on its own thread so a long-running clone doesn't
                # block the reader from picking up further commands
                threading.Thread(target=_run_updates_and_reindex, daemon=True).start()
            elif cmd == 'reindex':
                try:
                    count = doc_searcher.reindex()
                    print(f"[console] reindex: complete ({count} documents)")
                except Exception as e:
                    print(f"[console] reindex: error: {e}")
            else:
                print(f"[console] unknown command: {cmd!r} (try: update-docs, reindex)")
    except Exception as e:
        print(f"[console] reader stopped: {e}")


# Run the server
if __name__ == "__main__":
    import sys
    import os
    
    # Check if docs directory exists
    if not DOCS_DIR.exists():
        print(f"ERROR: Documentation directory not found: {DOCS_DIR}")
        print("Please ensure the docs directory exists with documentation files.")
        sys.exit(1)
    
    print(f"Starting CS2 Docs MCP Server...")
    print(f"Documentation path: {DOCS_DIR}")
    print(f"Indexed {len(doc_searcher.doc_index)} documents")
    print(f"Max concurrent requests: {MAX_CONCURRENT_REQUESTS}")
    print(f"File cache size: {FILE_CACHE_SIZE}")
    print(f"Result cache TTL: {RESULT_CACHE_TTL}s")
    print(f"Cleanup interval: {CLEANUP_INTERVAL}s")
    print(f"Optimizations: Request deduplication & result caching enabled")
    
    # Log initial resource usage
    ResourceMonitor.log_resources()
    
    # Get port from environment or use default
    port = int(os.getenv('PORT', 8080))

    # Start the console command reader (Pterodactyl schedules speak stdin).
    # Daemon thread so it dies with the process on shutdown.
    threading.Thread(target=_console_reader, daemon=True).start()

    # Auto-refresh docs on every boot. Runs in a background thread so the
    # MCP server can start serving immediately — the index hot-reloads once
    # the update completes. Set UPDATE_ON_STARTUP=0 to disable (useful for
    # local dev with pinned docs). Silently skipped if the update script
    # isn't present (e.g. the root docker-compose image doesn't ship it).
    _startup_script = os.getenv('UPDATE_SCRIPT', '/app/update-all.sh')
    if os.getenv('UPDATE_ON_STARTUP', '1') != '0' and os.path.isfile(_startup_script):
        print("[startup] auto-refresh: docs update queued in the background")
        threading.Thread(target=_run_updates_and_reindex, daemon=True).start()

    # Unique marker for Pterodactyl's `config.startup.done` detection. Must
    # be printed exactly once, right before mcp.run() blocks the main thread.
    print(f"Documentation MCP server ready on port {port}", flush=True)

    try:
        # Run the FastMCP server (blocks until shutdown)
        # FastMCP handles its own event loop
        mcp.run(transport="sse", port=port, host="0.0.0.0")
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        doc_searcher.clear_cache()
    except Exception as e:
        print(f"Server error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
