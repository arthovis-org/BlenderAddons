"""Minimal Figma REST API client using only the Python standard library.

Blender's bundled Python has no ``requests``, so everything here goes through
``urllib``.  The module is deliberately free of any ``bpy`` import so it can be
used from the standalone CLI as well as from inside Blender.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

API_ROOT = "https://api.figma.com/v1"
USER_AGENT = "figma_to_blender/0.1 (+https://github.com/arthovis-org/empty1)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"figma\.com/(?:design|file|proto|board)/([A-Za-z0-9]+)")


class FigmaError(Exception):
    """Raised for non-recoverable API failures (bad token, missing file...)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def parse_file_key(url_or_key: str) -> str:
    """Return the Figma file key from a URL or a bare key.

    Accepts ``https://www.figma.com/design/<KEY>/<name>?node-id=..`` and the
    older ``/file/<KEY>/..`` form.  Anything that does not look like a URL is
    returned unchanged (after stripping whitespace).
    """
    s = (url_or_key or "").strip()
    if not s:
        raise ValueError("Empty Figma file URL / key")
    m = _KEY_RE.search(s)
    if m:
        return m.group(1)
    if "figma.com" in s:
        raise ValueError("Could not find a file key in URL: %r" % s)
    # Bare key: strip anything after a slash or query string just in case.
    return re.split(r"[/?#]", s)[0]


def sanitize_id(node_id: str) -> str:
    """Make a Figma node id safe for use in a filename (``1:23`` -> ``1_23``)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", node_id)


def chunked(seq: Iterable[str], size: int) -> Iterable[List[str]]:
    buf: List[str] = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class FigmaClient:
    """Thin wrapper around the endpoints this add-on needs."""

    def __init__(
        self,
        token: str,
        timeout: float = 60.0,
        max_retries: int = 4,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        if not token:
            raise FigmaError("A Figma personal access token is required")
        self.token = token.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.ssl_context = ssl_context or ssl.create_default_context()

    # -- low level ---------------------------------------------------------

    def _request(self, url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
        """GET ``url`` and return the raw body, retrying on 429/5xx."""
        hdrs = {"User-Agent": USER_AGENT}
        if headers:
            hdrs.update(headers)
        delay = 1.0
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 or 500 <= e.code < 600:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    log.warning("Figma API %s on %s, retrying in %.1fs", e.code, url, wait)
                    time.sleep(wait)
                    delay = min(delay * 2, 30.0)
                    continue
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                raise FigmaError("HTTP %s for %s: %s" % (e.code, url, body), status=e.code) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                log.warning("Network error on %s (%s), retrying in %.1fs", url, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise FigmaError("Giving up on %s after %d attempts: %s" % (url, self.max_retries + 1, last_err))

    def _get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> dict:
        url = API_ROOT + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        raw = self._request(url, {"X-Figma-Token": self.token})
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise FigmaError("Invalid JSON from %s" % url) from e

    def download(self, url: str) -> bytes:
        """Download an arbitrary (already signed) asset URL."""
        return self._request(url)

    # -- high level --------------------------------------------------------

    def get_file_meta(self, file_key: str) -> dict:
        """``GET /files/{key}?depth=1`` - file name plus the list of pages."""
        return self._get_json("/files/%s" % file_key, {"depth": "1"})

    def list_pages(self, file_key: str) -> List[dict]:
        """Return ``[{"id": ..., "name": ...}, ...]`` for every CANVAS."""
        meta = self.get_file_meta(file_key)
        doc = meta.get("document") or {}
        pages = []
        for child in doc.get("children", []):
            if child.get("type") == "CANVAS":
                pages.append({"id": child["id"], "name": child.get("name", child["id"])})
        return pages

    def get_page(self, file_key: str, page_id: str) -> dict:
        """Fetch the full node tree of a page with ``geometry=paths``.

        Returns the CANVAS node (``document`` of the nodes response).
        """
        data = self._get_json(
            "/files/%s/nodes" % file_key,
            {"ids": page_id, "geometry": "paths"},
        )
        nodes = data.get("nodes") or {}
        entry = nodes.get(page_id)
        if not entry or not entry.get("document"):
            raise FigmaError("Page %s not found in file %s" % (page_id, file_key))
        return entry["document"]

    def export_images(
        self,
        file_key: str,
        node_ids: List[str],
        fmt: str = "svg",
        scale: float = 1.0,
        batch_size: int = 40,
    ) -> Dict[str, Optional[str]]:
        """Ask Figma to render nodes; returns ``{node_id: url_or_None}``.

        Failures of a whole batch are logged and the ids of that batch map
        to ``None`` so callers can skip them without aborting the import.
        """
        result: Dict[str, Optional[str]] = {}
        for batch in chunked(node_ids, batch_size):
            params = {"ids": ",".join(batch), "format": fmt}
            if fmt != "svg":
                params["scale"] = str(scale)
            else:
                params["svg_include_id"] = "false"
                params["svg_simplify_stroke"] = "true"
            try:
                data = self._get_json("/images/%s" % file_key, params)
            except FigmaError as e:
                log.error("Image export batch failed (%s); skipping %d nodes", e, len(batch))
                for nid in batch:
                    result[nid] = None
                continue
            if data.get("err"):
                log.error("Image export error: %s", data["err"])
            images = data.get("images") or {}
            for nid in batch:
                result[nid] = images.get(nid)
        return result
