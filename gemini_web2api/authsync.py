"""Receive a fresh Gemini session from the browser extension and persist it.

The `gemini-cookie-sync-extension` Chrome extension POSTs the live cookies plus
XSRF/build metadata to the proxy. This module writes them to the cookie file
(`gemini-auth.json`) and patches the `config.json` values that are derived from
the session: `cookie_file`, `auth_user`, `xsrf_token` and `gemini_bl`.

Both the single-file entry point and the modular server share this logic; the
caller is responsible for applying the returned ``updates`` to its own in-memory
``CONFIG`` so no restart is required.
"""
import json
import os
import tempfile
import time

# At least one of these must be present for the session to be usable.
SESSION_COOKIES = ("__Secure-1PSID", "__Secure-3PSID", "SID")

# 扩展把各 Google 子域的 cookie 拍平成一条 Cookie 串，其中 accounts.google.com /
# myaccount.google.com 的宿主级 cookie（OSID、__Secure-OSID 等）本不会发给
# gemini.google.com。混入后 Gemini 会判定会话不一致：页面 302 跳登录页，
# StreamGenerate 返回 401。落盘前剔除这些非 Gemini 作用域的名字。
FOREIGN_HOST_COOKIES = ("LSID", "OSID", "__Secure-OSID", "__Host-1PLSID", "__Host-3PLSID")


def parse_cookie(cookie):
    """Parse a Cookie header into a {name: value} dict (first wins)."""
    pairs = {}
    for part in cookie.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name and name not in pairs:
            pairs[name] = value
    return pairs


def normalize_payload(raw):
    """Validate an incoming sync payload.

    Returns ``(payload, error)``; exactly one of the two is ``None``.
    """
    if not isinstance(raw, dict):
        return None, "invalid JSON body"

    cookie = raw.get("cookie")
    if not isinstance(cookie, str) or not cookie.strip():
        return None, "missing 'cookie'"
    cookie = cookie.strip()

    cookies = parse_cookie(cookie)
    if any(name in cookies for name in FOREIGN_HOST_COOKIES):
        # 剔除后按 name=value 重新序列化，非 Gemini 作用域的宿主级 cookie 不再下发上游。
        cookies = {name: value for name, value in cookies.items() if name not in FOREIGN_HOST_COOKIES}
        cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
    if "SAPISID" not in cookies:
        return None, "cookie is missing SAPISID"
    if not any(name in cookies for name in SESSION_COOKIES):
        return None, "cookie is missing a session cookie (__Secure-1PSID/__Secure-3PSID/SID)"

    sapisid = raw.get("sapisid")
    if not isinstance(sapisid, str) or not sapisid.strip():
        sapisid = cookies.get("SAPISID", "")
    if not sapisid.strip():
        return None, "missing 'sapisid'"

    xsrf_token = raw.get("xsrf_token")
    if not isinstance(xsrf_token, str) or not xsrf_token.strip():
        return None, "missing 'xsrf_token' (open gemini.google.com, refresh, then retry)"

    auth_user = raw.get("auth_user")
    if isinstance(auth_user, bool):
        return None, "invalid 'auth_user'"
    if isinstance(auth_user, int):
        auth_user = str(auth_user)
    if auth_user is not None and not isinstance(auth_user, str):
        return None, "invalid 'auth_user'"
    if isinstance(auth_user, str):
        auth_user = auth_user.strip() or None

    gemini_bl = raw.get("gemini_bl")
    if gemini_bl is not None and not isinstance(gemini_bl, str):
        return None, "invalid 'gemini_bl'"
    if isinstance(gemini_bl, str):
        gemini_bl = gemini_bl.strip() or None

    return {
        "cookie": cookie,
        "sapisid": sapisid.strip(),
        "auth_user": auth_user,
        "xsrf_token": xsrf_token.strip(),
        "gemini_bl": gemini_bl,
    }, None


def load_json(path):
    """Return the JSON object stored at ``path``, or ``None`` if unusable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def atomic_write_json(path, data):
    """Write ``data`` as pretty JSON to ``path`` atomically."""
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".authsync-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows / restricted filesystems


def handle_sync(payload, auth_path, config_path):
    """Persist the session to ``auth_path`` and patch ``config_path``.

    Returns ``(result, error)``. ``result['updates']`` lists the config keys the
    caller should apply to its in-memory CONFIG. 落盘与变更判定都按全量内容比较
    （含轮转 token），会话因此始终是最新的。
    """
    updates = {
        "cookie_file": os.path.abspath(auth_path),
        "auth_user": payload["auth_user"],
        "xsrf_token": payload["xsrf_token"],
    }
    if payload.get("gemini_bl"):
        updates["gemini_bl"] = payload["gemini_bl"]

    session = {
        "cookie": payload["cookie"],
        "sapisid": payload["sapisid"],
        "auth_user": payload["auth_user"],
        "xsrf_token": payload["xsrf_token"],
        "gemini_bl": payload["gemini_bl"],
    }
    # 落盘保留完整 cookie（含 PSIDTS/SIDCC/AEC 等轮转 token）：Google 的图像能力会
    # 校验这些 token 的新鲜度，曾因删减它们导致"能聊天、不能出图"。
    # 变更判定按全量内容比较：轮转 token 变化也写盘并记录，保证会话始终是最新的。
    current = load_json(auth_path)
    auth_changed = current != session
    if auth_changed:
        atomic_write_json(auth_path, session)

    config_file = None
    config_changed = False
    if config_path and os.path.exists(config_path):
        current = load_json(config_path)
        config = dict(current or {})
        config.update(updates)
        config_changed = config != current
        if config_changed:
            atomic_write_json(config_path, config)
        config_file = os.path.abspath(config_path)

    return {
        "ok": True,
        "auth_file": os.path.abspath(auth_path),
        "config_file": config_file,
        "updates": updates,
        "changed": auth_changed or config_changed,
        "saved_at": int(time.time()),
    }, None
