"""Gemini generated-image support.

Gemini's web backend returns generated images *inside* the StreamGenerate
response, but not as inline base64. Each image is described by a JSPB node that
looks like::

    [null, 1, "<filename>.png", "https://lh3.googleusercontent.com/gg-dl/<token>",
     null, "<opaque id>", null, null, 2, [<ts>, <seq>], null, "image/png",
     null, null, null, [<width>, <height>, <bytes>]]

The `lh3.googleusercontent.com/gg-dl/...` URL is *not* directly downloadable: it
302-redirects through `work.fife.usercontent.google.com/rd-gg-dl/...` and only
resolves when the request carries the Gemini session (Referer/Origin + cookies)
and a browser-like TLS fingerprint. ``download_image`` performs that indirection
on a best-effort basis and returns ``None`` when the upstream refuses.

This module is intentionally self-contained (no dependency on the rest of the
package) so both the single-file entry point and the modular server can reuse it.
"""
import hashlib
import json
import re
import ssl
import time
import urllib.request

IMAGE_URL_PREFIX = "https://lh3.googleusercontent.com/"
IMAGE_REF_RE = re.compile(r"https?://googleusercontent\.com/image_generation_content/\d+_\d+")

_ssl_ctx = None


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


# ─── Extraction ──────────────────────────────────────────────────────────────

def _is_image_node(node) -> bool:
    return (
        isinstance(node, list)
        and len(node) >= 4
        and node[1] == 1
        and isinstance(node[2], str)
        and isinstance(node[3], str)
        and node[3].startswith(IMAGE_URL_PREFIX)
    )


def _walk_images(node, out):
    if isinstance(node, list):
        if _is_image_node(node):
            out.append(node)
        for child in node:
            _walk_images(child, out)


def _first_str(node, prefix):
    for value in node:
        if isinstance(value, str) and value.startswith(prefix):
            return value
    return None


def _first_dims(node):
    for value in node:
        if (
            isinstance(value, list)
            and len(value) == 3
            and all(isinstance(i, int) for i in value)
        ):
            return value
    return None


def extract_generated_images(raw: str) -> list:
    """Parse a raw StreamGenerate response and return generated-image descriptors.

    Each item is a dict with ``url``, ``filename``, ``mime``, ``width``,
    ``height``, ``size``, ``image_id`` and the conversation ids
    ``cid``/``rid``/``rcid`` (used by the download RPC, kept for completeness).
    """
    images = []
    seen = set()
    for line in (raw or "").split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner = json.loads(arr[0][2])
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
        if not isinstance(inner, list):
            continue

        nodes = []
        _walk_images(inner, nodes)
        if not nodes:
            continue

        cid = rid = rcid = None
        try:
            if isinstance(inner[1], list) and len(inner[1]) >= 2:
                cid, rid = inner[1][0], inner[1][1]
            if isinstance(inner[4], list) and inner[4] and isinstance(inner[4][0], list):
                rcid = inner[4][0][0] if inner[4][0] else None
        except (IndexError, TypeError):
            pass

        for node in nodes:
            url = node[3]
            if url in seen:
                continue
            seen.add(url)
            dims = _first_dims(node)
            images.append({
                "url": url,
                "filename": node[2],
                "mime": _first_str(node, "image/") or "image/png",
                "width": dims[0] if dims else None,
                "height": dims[1] if dims else None,
                "size": dims[2] if dims else None,
                "image_id": node[5] if len(node) > 5 and isinstance(node[5], str) else None,
                "cid": cid,
                "rid": rid,
                "rcid": rcid,
            })
    return images


def apply_images_to_text(text: str, images: list) -> str:
    """Replace Gemini's `image_generation_content` placeholder with markdown images."""
    if not images:
        return text
    cleaned = IMAGE_REF_RE.sub("", text or "").strip()
    links = "\n\n".join(f"![image]({img['url']})" for img in images)
    if cleaned:
        return f"{cleaned}\n\n{links}"
    return links


def conversation_context(images: list):
    """Return the Gemini Web continuation metadata for a generated image reply."""
    for image in images or []:
        cid = image.get("cid")
        rid = image.get("rid")
        if cid and rid:
            return {"cid": cid, "rid": rid}
    return None


# ─── Download (best effort) ──────────────────────────────────────────────────

def _sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def _build_headers(cookie: str = None, sapisid: str = None) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    if sapisid:
        headers["Authorization"] = _sapisidhash(sapisid)
    return headers


def _http_get(url: str, headers: dict, proxy: str = None, timeout: int = 60):
    """GET ``url`` returning (status, content_type, body_bytes).

    Prefers ``curl_cffi`` when installed because Google's image CDN rejects
    requests whose TLS fingerprint is not browser-like.
    """
    try:
        from curl_cffi import requests as creq  # optional dependency
        sess = creq.Session(impersonate="chrome", proxy=proxy)
        resp = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.headers.get("content-type", ""), resp.content
    except ImportError:
        pass
    except Exception:
        pass

    try:
        import httpx  # optional dependency
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            return resp.status_code, resp.headers.get("content-type", ""), resp.content
    except ImportError:
        pass
    except Exception:
        pass

    req = urllib.request.Request(url, headers=headers)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
        )
        resp = opener.open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=timeout)
    return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def download_image(url: str, proxy: str = None, cookie: str = None, sapisid: str = None,
                   timeout: int = 20, max_hops: int = 3):
    """Resolve and download a generated image.

    Returns ``(bytes, mime)`` on success or ``(None, None)`` when the upstream
    refuses (Google signs these URLs against the live browser session, so a
    server-side fetch can legitimately fail with 403).
    """
    if not url or not url.startswith(IMAGE_URL_PREFIX):
        return None, None

    headers = _build_headers(cookie, sapisid)
    current = url if "=d-I?alr=yes" in url else url + "=d-I?alr=yes"
    for _ in range(max_hops + 1):
        try:
            status, content_type, body = _http_get(current, headers, proxy, timeout)
        except Exception:
            return None, None
        if status != 200:
            return None, None
        if content_type.startswith("image/"):
            return body, content_type.split(";")[0].strip()
        next_url = body.decode("utf-8", "replace").strip()
        if not next_url.startswith("http") or next_url == current:
            return None, None
        current = next_url
    return None, None
