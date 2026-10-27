Gemini Cookie Sync v1.1

Purpose:
- Read cookies for the current Google/Gemini session.
- Extract the XSRF token named SNlM0e from the Gemini page.
- Extract gemini_bl from cfb2h or from page requests when available.
- Push the session straight to a running gemini-web2api server (which updates
  gemini-auth.json and config.json), or export gemini-auth.json locally.
- Optionally repeat the push on a timer (chrome.alarms).

Installation:
1. Open `chrome://extensions`
2. Enable Developer mode
3. Click Load unpacked
4. Select this folder
5. Open `https://gemini.google.com/app`, sign in, and refresh the page

Automatic sync:
1. Start the server: `python gemini_web2api.py`
2. Click the extension icon
3. Set Server URL (default http://127.0.0.1:8081, must match `port` in config.json)
4. If config.json sets `sync_token`, enter the same value in Sync token
5. Click Sync to server now
6. Tick "Sync automatically in the background" and set the interval to sync on a schedule

Manual export:
- Click Inspect session
- Click Export gemini-auth.json (manual)
- Move the file next to gemini_web2api.py if you did not use automatic sync

Files:
- auth.js        shared session/cookie/page logic
- background.js  periodic sync (alarms) + message handler
- popup.html/js  settings UI
- manifest.json  MV3 manifest (needs alarms, storage, and localhost host permissions)

Server endpoint:
POST /v1/auth/sync
  { "cookie": "...", "sapisid": "...", "auth_user": "0",
    "xsrf_token": "...", "gemini_bl": "..." }
  optional header: X-Sync-Token: <config.json sync_token>

Security:
The generated file and the pushed payload represent the real Google session and
must be treated as secret. Do not send it, print it, or commit it to Git.
The endpoint is not protected by api_keys; set `sync_token` in config.json to
require a shared secret, and note that requests carrying a non-extension
`Origin` header are rejected.
