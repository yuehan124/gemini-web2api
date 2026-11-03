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
VIDEO_FILE_RE = re.compile(r"\.(?:mp4|m4v|webm|mov|avi|mkv)(?:[?#]|$)", re.I)

# Used when Gemini answers an image prompt with a video.  The instruction is
# appended only on retry so normal image prompts are sent to Gemini unchanged.
IMAGE_ONLY_INSTRUCTION = (
    "\n\n[Return one still image only. Do not generate a video, animation, "
    "audio, or a link to any of them.]"
)

# Conservative video-intent detection used to reject prompts that can only
# produce a video.  Prompts that also mention an image/still/frame/thumbnail
# are left alone so "make a video thumbnail" still reaches the image path.
VIDEO_REQUEST_RE = re.compile(
    r"(?:生成|制作|创建|做|拍|来|给我)\s*(?:一|一个|一段|个)?\s*"
    r"(?:视频|短片|动画|影片|动图)(?!游戏|封面|缩略图|帧|画面|图片|海报)"
    r"|(?:generate|create|make|produce|render)\s+(?:me\s+)?(?:a|an|one|some)?\s*"
    r"(?:video|animation|movie|clip|gif)\b"
    r"(?!\s*(?:game|frame|thumbnail|poster|cover|image|picture|still))"
    r"|(?:text|image)[- ]to[- ]video",
    re.IGNORECASE,
)
IMAGE_REQUEST_RE = re.compile(
    r"(?:图片|照片|图像|画面|静帧|海报|封面|缩略图|插画|插图|配图|绘图|制图|绘画|帧)"
    r"|(?:image|photo|picture|still|poster|thumbnail|illustration|drawing|frame)",
    re.IGNORECASE,
)
NEGATION_RE = re.compile(
    r"(?:不要|别|无需|不需要|不用|禁止|\bno\b|\bdon't\b|\bdo not\b|\bwithout\b|\bnot\b)",
    re.IGNORECASE,
)

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
        and not _is_video_node(node)
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


def _iter_response_inners(raw: str):
    """Yield the decoded JSPB payload of every StreamGenerate chunk."""
    for line in (raw or "").split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            arr = json.loads(line)
            inner = json.loads(arr[0][2])
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
        if isinstance(inner, list):
            yield inner


def _conversation_ids(inner: list):
    cid = rid = rcid = None
    try:
        if isinstance(inner[1], list) and len(inner[1]) >= 2:
            cid, rid = inner[1][0], inner[1][1]
        if isinstance(inner[4], list) and inner[4] and isinstance(inner[4][0], list):
            rcid = inner[4][0][0] if inner[4][0] else None
    except (IndexError, TypeError):
        pass
    return cid, rid, rcid


def extract_generated_images(raw: str) -> list:
    """Parse a raw StreamGenerate response and return generated-image descriptors.

    Each item is a dict with ``url``, ``filename``, ``mime``, ``width``,
    ``height``, ``size``, ``image_id`` and the conversation ids
    ``cid``/``rid``/``rcid`` (used by the download RPC, kept for completeness).
    """
    images = []
    seen = set()
    for inner in _iter_response_inners(raw):
        nodes = []
        _walk_images(inner, nodes)
        if not nodes:
            continue

        cid, rid, rcid = _conversation_ids(inner)

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


def _is_video_node(node) -> bool:
    """Return True for a generated-video JSPB node.

    Video nodes use the same shape as image nodes but carry a ``video/*`` mime
    type and/or an ``.mp4``/``.webm`` filename.  Some Gemini builds also expose
    video through a ``video_generation_content`` URL instead of a media node.
    """
    if not isinstance(node, list):
        return False
    if _first_str(node, "video/"):
        return True
    if len(node) < 4 or not isinstance(node[2], str) or not isinstance(node[3], str):
        return False
    if not node[3].startswith(("http://", "https://")):
        return False
    haystack = f"{node[2]} {node[3]}".lower()
    return "video_generation_content" in haystack or bool(VIDEO_FILE_RE.search(haystack))


def _walk_videos(node, out):
    if isinstance(node, list):
        if _is_video_node(node):
            out.append(node)
        for child in node:
            _walk_videos(child, out)


def _video_mime(node) -> str:
    mime = _first_str(node, "video/")
    if mime:
        return mime
    haystack = " ".join(v for v in node if isinstance(v, str)).lower()
    if ".webm" in haystack:
        return "video/webm"
    if ".mov" in haystack:
        return "video/quicktime"
    if ".m4v" in haystack:
        return "video/x-m4v"
    return "video/mp4"


def extract_generated_videos(raw: str) -> list:
    """Return generated-video descriptors found in a StreamGenerate response."""
    videos = []
    seen = set()
    for inner in _iter_response_inners(raw):
        nodes = []
        _walk_videos(inner, nodes)
        if not nodes:
            continue

        cid, rid, rcid = _conversation_ids(inner)
        for node in nodes:
            url = node[3] if len(node) > 3 and isinstance(node[3], str) else None
            key = url or f"node:{id(node)}"
            if key in seen:
                continue
            seen.add(key)
            videos.append({
                "url": url,
                "filename": node[2] if len(node) > 2 and isinstance(node[2], str) else None,
                "mime": _video_mime(node),
                "cid": cid,
                "rid": rid,
                "rcid": rcid,
            })

    # Fallback for builds that only expose a video placeholder/link in the text.
    raw_text = (raw or "").replace("\\/", "/")
    for match in re.finditer(r'https?://[^\s"\'<>\\]+', raw_text):
        url = match.group(0)
        if "video_generation_content" not in url and not VIDEO_FILE_RE.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        videos.append({
            "url": url,
            "filename": None,
            "mime": _video_mime([url]),
            "cid": None,
            "rid": None,
            "rcid": None,
        })
    return videos


def with_image_only_instruction(prompt: str) -> str:
    """Append a still-image constraint used when Gemini returns a video."""
    return (prompt or "").rstrip() + IMAGE_ONLY_INSTRUCTION


def is_video_request(prompt: str) -> bool:
    """Return True when the prompt is unambiguously asking for a video.

    Used by the image endpoint to fail fast instead of letting Gemini generate
    a video that the OpenAI Images response cannot represent.
    """
    prompt = prompt or ""
    return (
        bool(VIDEO_REQUEST_RE.search(prompt))
        and not IMAGE_REQUEST_RE.search(prompt)
        and not NEGATION_RE.search(prompt)
    )


def image_error(message: str, code: str, error_type: str = "invalid_request_error", **extra) -> dict:
    """Build an OpenAI-compatible error object with a stable machine code."""
    error = {"message": message, "type": error_type, "code": code}
    error.update(extra)
    return error


def classify_image_failure(raw: str, text: str = None) -> dict:
    """Describe why an image request produced no image.

    The returned object is ready to be embedded under the top-level ``error``
    key, so clients can show a specific message and branch on ``code``.
    """
    videos = extract_generated_videos(raw)
    if videos:
        mime = videos[0].get("mime") or "video/*"
        error = image_error(
            f"Gemini returned a video ({mime}) instead of an image. "
            "This endpoint only generates still images; retry with a prompt "
            "that explicitly asks for an image, or use a video-capable endpoint.",
            "gemini_video_response",
            "upstream_unsupported_media",
            media_type=mime,
        )
        if text:
            error["detail"] = text[:1000]
        return error
    if text:
        return image_error(text, "gemini_text_response", "upstream_error")
    return image_error(
        "Gemini returned neither an image nor text. The prompt may have "
        "triggered an unsupported output such as a video; retry with an "
        "explicit still-image prompt.",
        "gemini_empty_response",
        "upstream_error",
    )


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
    """按 Chrome 实际请求构造下载头；``sapisid`` 保留签名仅为兼容调用方。

    lh3 的最终图片跳对浏览器指纹头敏感：缺少 Sec-Fetch-*/sec-ch-ua 时稳定返回
    403 机器人页（实测同一 URL 加全量浏览器头即可 200），而 SAPISIDHASH 认证头
    既非必需，也会让请求区别于浏览器的匿名图片请求。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "image",
        "sec-ch-ua": '"Chromium";v="156", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _curl_get(url: str, headers: dict, proxy: str, timeout: int):
    """走系统 curl 的 GET：实测同一代理对 curl 的 TLS 握手成功率显著高于 urllib（约 6/10）。

    Google 的隧道端点对 TLS ClientHello 特征敏感，curl 触发 h2 ALPN 握手可减少被
    代理侧重置；结果码与内容类型经 -w 输出，响应体写入临时文件，保持逐跳重定向
    由 Python 侧行走（text/plain 中转响应不会被 -L 跟随）。
    """
    import os
    import subprocess
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="gemini-img-", suffix=".bin")
    os.close(fd)
    args = ["curl", "-sS", "-L", "--max-redirs", "5", "-m", str(timeout)]
    if proxy:
        args += ["-x", proxy]
    for key, value in headers.items():
        args += ["-H", f"{key}: {value}"]
    args += ["-o", tmp, "-w", "%{http_code}\n%{content_type}", url]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout + 10)
        meta = proc.stdout.decode("utf-8", "replace").strip().split("\n")
        status = int(meta[0]) if meta and meta[0].strip().isdigit() else 0
        ctype = meta[1].strip() if len(meta) > 1 else ""
        with open(tmp, "rb") as f:
            body = f.read()
        return status, ctype, body
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _http_get(url: str, headers: dict, proxy: str = None, timeout: int = 60):
    """GET ``url`` returning (status, content_type, body_bytes).

    优先系统 curl（代理兼容性最好），其次 curl_cffi / httpx（可选依赖），最后 urllib。
    """
    try:
        return _curl_get(url, headers, proxy, timeout)
    except FileNotFoundError:
        pass
    except Exception:
        pass

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


def _hop_get(current: str, headers: dict, proxy: str, timeout: int, tries: int = 5, delay: int = 8,
             deadline: float = None):
    """带原地重试的单跳请求：当前 VPN 代理对 Google CDN 的连接有约四成瞬时失败率，
    且连续快速重连会触发更长的封锁窗口，因此失败时按 ``delay`` 秒间隔重试当前跳。
    ``deadline``（time.monotonic 时间戳）到点即止损，保证调用方的超时预算不被拖穿。
    仅重试 CDN 下载，不涉及 Gemini 接口调用，避免触发生成侧的风控。"""
    last = None
    for i in range(max(1, tries)):
        try:
            return _http_get(current, headers, proxy, timeout)
        except Exception as e:  # 连接/握手瞬时失败，继续重试
            last = e
        if (deadline is not None and time.monotonic() >= deadline) or i >= tries - 1:
            break
        time.sleep(delay)
    raise last if last else RuntimeError("download hop failed")


def download_image(url: str, proxy: str = None, cookie: str = None, sapisid: str = None,
                   timeout: int = 30, max_hops: int = 6, attempts: int = 4, retry_delay: int = 10,
                   max_seconds: int = 110):
    """Resolve and download a generated image.

    Returns ``(bytes, mime)`` on success or ``(None, None)`` when the upstream
    refuses (Google signs these URLs against the live browser session, so a
    server-side fetch can legitimately fail with 403).

    当前代理对 lh3.googleusercontent.com 的连接存在间歇性失败（实测约 4/10），
    跳转链共 3 跳，任何一跳失败都会作废整次下载；``_hop_get`` 对每跳做原地重试，
    外层再按 ``attempts``/``retry_delay`` 整链兜底重试；``max_seconds`` 设总时长止损
    （默认 110 秒），保证在调用方的超时预算内收敛。只重试图片下载、不涉及
    任何 Gemini 接口调用，避免触发生成侧的风控。实测对节奏敏感：密集重连会
    触发更长的封锁窗口，当前节奏（每跳最多 5 次、间隔 8 秒）在坏窗口也能拿到图。
    """
    if not url or not url.startswith(IMAGE_URL_PREFIX):
        return None, None

    t0 = time.monotonic()
    import sys

    headers = _build_headers(cookie, sapisid)
    entry = url if "=d-I?alr=yes" in url else url + "=d-I?alr=yes"
    deadline = time.monotonic() + max(20, max_seconds)
    for attempt in range(max(1, attempts)):
        if attempt > 0 and time.monotonic() >= deadline:
            break
        current = entry
        broken = False
        for hop in range(max_hops):
            started = time.monotonic()
            try:
                status, content_type, body = _hop_get(current, headers, proxy, timeout, deadline=deadline)
            except Exception as exc:
                # 诊断输出进入服务 stderr（nohup 汇聚入 logs/console.log），仅记录类型与耗时。
                print(f"tl={time.monotonic()-t0:.1f}s chain{attempt+1} hop{hop+1} EXC "
                      f"{type(exc).__name__} after {time.monotonic()-started:.1f}s",
                      file=sys.stderr, flush=True)
                broken = True
                break
            print(f"tl={time.monotonic()-t0:.1f}s chain{attempt+1} hop{hop+1} status={status} "
                  f"type={content_type} bytes={len(body)} wait={time.monotonic()-started:.1f}s",
                  file=sys.stderr, flush=True)
            if status != 200:
                broken = True
                break
            if content_type.startswith("image/"):
                mime = content_type.split(";")[0].strip()
                return body, mime
            next_url = body.decode("utf-8", "replace").strip().strip('"')
            if not next_url.startswith("http") or next_url == current:
                print(f"tl={time.monotonic()-t0:.1f}s chain{attempt+1} hop{hop+1} dead-end body",
                      file=sys.stderr, flush=True)
                broken = True
                break
            current = next_url
        if attempt < attempts - 1 and time.monotonic() < deadline:
            time.sleep(retry_delay)
        if not broken:
            break
    return None, None


def to_png(data: bytes, mime: str = None):
    """统一转为 PNG 字节；下载结果实测可能是 JPEG，调用方按 OpenAI Images 约定只认 PNG。

    返回 ``(bytes, "image/png")``；Pillow 不可用或转换失败时原样返回 + 原始 mime。
    """
    if not data or (mime or "").startswith("image/png"):
        if data and (data[:8] == b"\x89PNG\r\n\x1a\n"):
            return data, "image/png"
    try:
        import io as _io
        from PIL import Image

        with Image.open(_io.BytesIO(data)) as image:
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")
            out = _io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue(), "image/png"
    except Exception:
        return data, (mime or "application/octet-stream")


def store_image(data: bytes, directory: str = None):
    """把下载并转码后的图片落盘到本地托管目录，返回文件名（按 URL 内容唯一化）。"""
    import hashlib
    import os

    directory = directory or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "downloads")
    os.makedirs(directory, exist_ok=True)
    name = hashlib.sha1(data).hexdigest()[:24] + ".png"
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return name
