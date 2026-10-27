# Gemini Cookie Sync Setup

Short guide for extracting fresh Gemini auth data and applying it to `gemini-web2api`.

## What this extension handles

The extension reads the current signed-in Gemini session and collects:

- Google session cookies
- `SAPISID`
- `SNlM0e` (`xsrf_token`)
- `cfb2h` (`gemini_bl`)
- `auth_user`

It can either export them locally as `gemini-auth.json` or POST them to a
running `gemini-web2api` server, which writes the file and patches `config.json`.

## Install

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select the `gemini-cookie-sync-extension` folder
5. Open [https://gemini.google.com/app](https://gemini.google.com/app)
6. Sign in and refresh the page

7. Open the extension and click **Inspect session**
8. Confirm the session looks ready

Expected ready state:

```text
XSRF / SNlM0e: present
gemini_bl / cfb2h: present
Session and XSRF are ready for export.
```

Then either use **Automatic sync** (below) or click **Export gemini-auth.json (manual)**.

## Automatic sync (recommended)

Version 1.1 can push the session straight to a running `gemini-web2api`
server, which then updates `gemini-auth.json` **and** `config.json` for you —
no manual file moving, no `jq`, no restart.

1. Start the server: `python gemini_web2api.py` (reads `config.json`)
2. Click the extension icon
3. Set **Server URL** to the server address (default `http://127.0.0.1:8081`;
   must match `port` in `config.json`)
4. If `config.json` sets `sync_token`, paste the same value into **Sync token**
5. Click **Sync to server now**

The popup reports which files were written and which config keys changed. The
server applies the new values to its in-memory config immediately, so the next
API call already uses the fresh session.

### Periodic sync

Tick **Sync automatically in the background** and set an interval (minutes).
The extension then re-pushes the session on that schedule using
`chrome.alarms`.

Notes:

- The server endpoint is `POST /v1/auth/sync`. It is **not** protected by
  `api_keys` (it is not a model call); instead it uses the optional
  `sync_token` plus an `Origin` check that rejects normal web pages.
- A fresh XSRF token (`SNlM0e`) can only be read from a signed-in
  `gemini.google.com` page. Keep a Gemini tab open (or allow the extension's
  page fetch to succeed); if neither works the sync fails loudly and writes
  nothing, rather than storing a stale session.
- `lastSync` (time, status, message) is kept in the extension's local storage
  and shown in the popup.

## Apply it manually in `gemini-web2api` (fallback)

Use this if you prefer a file or cannot reach the server from the browser.

Move the exported file into the project:

```bash
cd /path/to/gemini-web2api

WIN_HOME=$(wslpath "$(powershell.exe -NoProfile -Command '[Environment]::GetFolderPath(\"UserProfile\")' | tr -d '\r')")
cp "$WIN_HOME/Downloads/gemini-auth.json" ./gemini-auth.json
chmod 600 gemini-auth.json
```

Update `config.json`:

```bash
cd /path/to/gemini-web2api

AUTH_FILE="$(pwd)/gemini-auth.json"
tmp=$(mktemp)

jq \
  --arg auth_file "$AUTH_FILE" \
  --slurpfile auth "$AUTH_FILE" \
  '
    .cookie_file = $auth_file
    | .auth_user = $auth[0].auth_user
    | .xsrf_token = $auth[0].xsrf_token
    | if (($auth[0].gemini_bl // "") | length) > 0
      then .gemini_bl = $auth[0].gemini_bl
      else .
      end
  ' config.json > "$tmp" &&
mv "$tmp" config.json

chmod 600 config.json
```

Quick check:

```bash
jq '{
  cookie_file,
  auth_user,
  xsrf_token_set: ((.xsrf_token // "") | length > 0),
  gemini_bl_set: ((.gemini_bl // "") | length > 0)
}' config.json
```

## Restart and test

Only needed for the manual file flow: if you used **Sync to server now**, the
running server already picked up the new session and you can skip the restart.

```bash
systemctl --user restart gemini-proxy
```

```bash
curl -sS http://127.0.0.1:10012/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gemini-3.1-pro",
    "messages": [
      {
        "role": "user",
        "content": "Reply exactly with: authenticated-ok"
      }
    ]
  }' | jq
```

## Keep it secret

`gemini-auth.json` contains a real Google session. Do not share it, print it, or commit it to Git.
