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

# Google rotates these companion cookies on any background Google activity, so
# their values change on nearly every extension push even when the session
# itself is unchanged. They are never used to authenticate (the API relies on
# SAPISID for the SAPISIDHASH and the stable session cookie), so we ignore them
# when deciding whether a sync actually changed the session.
VOLATILE_COOKIES = frozenset((
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
    "__Secure-1PSIDRTS",
    "__Secure-3PSIDRTS",
    "AEC",
    "NID",
    "SIDCC",
    "COMPASS",
))


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


def stable_cookie(cookie):
    """Drop volatile rotating cookies and return the remaining cookie string.

    Only cookies that matter for authentication are kept, so the persisted
    session stays byte-stable across background syncs and does not carry
    short-lived Google housekeeping cookies into the file.
    """
    pairs = parse_cookie(cookie)
    return "; ".join(
        f"{name}={value}"
        for name, value in pairs.items()
        if name not in VOLATILE_COOKIES
    )


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
    caller should apply to its in-memory CONFIG. Files whose content would not
    change are left untouched and ``result['changed']`` is ``False``.
    """
    updates = {
        "cookie_file": os.path.abspath(auth_path),
        "auth_user": payload["auth_user"],
        "xsrf_token": payload["xsrf_token"],
    }
    if payload.get("gemini_bl"):
        updates["gemini_bl"] = payload["gemini_bl"]

    session = {
        "cookie": stable_cookie(payload["cookie"]),
        "sapisid": payload["sapisid"],
        "auth_user": payload["auth_user"],
        "xsrf_token": payload["xsrf_token"],
        "gemini_bl": payload["gemini_bl"],
    }
    # Volatile rotating cookies are stripped above, so the stored cookie only
    # changes when the real session does; a re-push of the same session leaves
    # the file (and its mtime) untouched.
    auth_changed = load_json(auth_path) != session
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
