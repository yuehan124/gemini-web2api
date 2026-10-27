// auth.js — shared session collection for the popup and the background worker.
// Loaded as a classic script by popup.html and via importScripts() in
// background.js, so it must not use ES module syntax.

const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];
const LEGACY_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];

const EXPORT_ORDER = [
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "SAPISID",
  "LSID",
  "OSID",
  "SIDCC",
  "AEC",
  "NID",
  "COMPASS",
  "__Secure-1PAPISID",
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
  "__Secure-1PSIDRTS",
  "__Secure-3PAPISID",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "__Secure-3PSIDCC",
  "__Secure-3PSIDRTS",
  "__Secure-OSID",
  "__Host-1PLSID",
  "__Host-3PLSID"
];

const LOOKUP_URLS = [
  "https://gemini.google.com/app",
  "https://accounts.google.com/",
  "https://www.google.com/",
  "https://google.com/"
];

const DEFAULT_SETTINGS = {
  serverUrl: "http://127.0.0.1:8081",
  syncToken: "",
  autoSync: true,
  intervalMinutes: 25
};

const SYNC_PATH = "/v1/auth/sync";

// ─── Settings ────────────────────────────────────────────────────────────────

async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULT_SETTINGS);
  return { ...DEFAULT_SETTINGS, ...stored };
}

async function saveSettings(patch) {
  await chrome.storage.local.set(patch);
}

async function getLastSync() {
  const stored = await chrome.storage.local.get({ lastSync: null });
  return stored.lastSync;
}

// ─── Cookies ─────────────────────────────────────────────────────────────────

function normalizeDomain(domain = "") {
  return domain.replace(/^\./, "").toLowerCase();
}

function isGoogleCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return domain === "google.com" || domain.endsWith(".google.com");
}

function cookieKey(cookie) {
  const partition = cookie.partitionKey ? JSON.stringify(cookie.partitionKey) : "";
  return [
    cookie.storeId || "",
    cookie.name,
    cookie.domain,
    cookie.path,
    partition
  ].join("|");
}

function scoreCookie(cookie) {
  const domain = (cookie.domain || "").toLowerCase();
  let score = 0;

  if (domain === ".google.com") score += 120;
  else if (domain === "google.com") score += 110;
  else if (domain === ".gemini.google.com") score += 100;
  else if (domain === "gemini.google.com") score += 95;
  else if (domain === ".accounts.google.com") score += 80;
  else if (domain === "accounts.google.com") score += 75;
  else if (domain.endsWith(".google.com")) score += 40;

  if (cookie.path === "/") score += 10;
  if (cookie.secure) score += 3;
  if (cookie.httpOnly) score += 2;
  if (!cookie.partitionKey) score += 2;
  if (!cookie.session) score += 1;

  return score;
}

async function readGoogleCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const deduped = new Map();

  for (const store of stores) {
    const queries = [
      chrome.cookies.getAll({ storeId: store.id }),
      ...LOOKUP_URLS.map((url) => chrome.cookies.getAll({ storeId: store.id, url }))
    ];

    const results = await Promise.allSettled(queries);

    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      for (const cookie of result.value) {
        if (!isGoogleCookie(cookie) || !cookie.value) continue;
        deduped.set(cookieKey(cookie), cookie);
      }
    }
  }

  return [...deduped.values()];
}

function selectBestCookies(cookies) {
  const selected = new Map();

  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((cookie) => cookie.name === name && cookie.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));

    if (candidates.length > 0) selected.set(name, candidates[0]);
  }

  return selected;
}

function validateSelection(selected) {
  const missingCore = CORE_REQUIRED.filter((name) => !selected.has(name));
  const sessionCookie = SESSION_ALTERNATIVES.find((name) => selected.has(name));
  return {
    missingCore,
    sessionCookie,
    valid: missingCore.length === 0 && Boolean(sessionCookie)
  };
}

function listPresent(selected, names) {
  return names.filter((name) => selected.has(name));
}

function summarizeAvailable(cookies) {
  return [...new Set(cookies
    .map((cookie) => cookie.name)
    .filter((name) => /SID|APISID|LSID|OSID|COMPASS/.test(name)))]
    .sort();
}

function buildCookieString(selected) {
  return EXPORT_ORDER
    .filter((name) => selected.has(name))
    .map((name) => `${name}=${selected.get(name).value}`)
    .join("; ");
}

// ─── Page metadata (XSRF / build label) ──────────────────────────────────────

function getActiveGeminiAccount(tabs) {
  for (const tab of tabs) {
    try {
      const url = new URL(tab.url || "");
      if (url.hostname !== "gemini.google.com") continue;
      const match = url.pathname.match(/^\/u\/(\d+)(?:\/|$)/);
      return match ? match[1] : null;
    } catch {
      // Ignore malformed or unavailable URLs.
    }
  }
  return null;
}

function chooseGeminiTab(tabs) {
  return tabs.find((tab) => tab.active) || tabs[0] || null;
}

async function readGeminiPageMetadata(tabs) {
  const tab = chooseGeminiTab(tabs);
  if (!tab?.id) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "No gemini.google.com tab is open."
    };
  }

  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => {
        const wiz = globalThis.WIZ_global_data || {};
        const html = document.documentElement?.innerHTML || "";

        const decode = (value) => {
          if (!value) return null;
          try {
            return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
          } catch {
            return value
              .replace(/\\u003d/gi, "=")
              .replace(/\\u0026/gi, "&")
              .replace(/\\u003c/gi, "<")
              .replace(/\\u003e/gi, ">");
          }
        };

        const regexValue = (name) => {
          const patterns = [
            new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`),
            new RegExp(`\\\\"${name}\\\\"\\s*:\\s*\\\\"([^"\\n]+)\\\\"`)
          ];
          for (const pattern of patterns) {
            const match = html.match(pattern);
            if (match?.[1]) return decode(match[1]);
          }
          return null;
        };

        let resourceBl = null;
        try {
          const resources = performance.getEntriesByType("resource");
          for (const entry of resources) {
            if (!entry?.name?.includes("gemini.google.com")) continue;
            const url = new URL(entry.name);
            const bl = url.searchParams.get("bl");
            if (bl) {
              resourceBl = bl;
              break;
            }
          }
        } catch {
          // Ignore performance API failures.
        }

        const xsrfToken = wiz.SNlM0e || regexValue("SNlM0e");
        const geminiBl = wiz.cfb2h || resourceBl || regexValue("cfb2h");

        let source = null;
        if (wiz.SNlM0e || wiz.cfb2h) source = "WIZ_global_data";
        else if (resourceBl) source = "performance";
        else if (xsrfToken || geminiBl) source = "page-html";

        return {
          xsrfToken: xsrfToken || null,
          geminiBl: geminiBl || null,
          source,
          url: location.href
        };
      }
    });

    return result?.[0]?.result || {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Failed to read Gemini page data."
    };
  } catch (error) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: error?.message || String(error)
    };
  }
}

// Fallback used when no Gemini tab is open: fetch the app page and scrape the
// same values out of the HTML. Cookies are attached because the extension holds
// host permissions for *.google.com.
async function fetchGeminiPageMetadata() {
  try {
    const response = await fetch("https://gemini.google.com/app", {
      credentials: "include",
      redirect: "follow"
    });
    if (!response.ok) {
      return { xsrfToken: null, geminiBl: null, source: null, error: `HTTP ${response.status}` };
    }

    const html = await response.text();
    const decode = (value) => (value || "")
      .replace(/\\u003d/gi, "=")
      .replace(/\\u0026/gi, "&")
      .replace(/\\u003c/gi, "<")
      .replace(/\\u003e/gi, ">");

    const pick = (name) => {
      const match = html.match(new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`));
      return match ? decode(match[1]) : null;
    };

    let geminiBl = pick("cfb2h");
    if (!geminiBl) {
      const match = html.match(/boq[_a-z-]*_\d{8}\.\d{2}_p\d+/);
      geminiBl = match ? match[0] : null;
    }

    const xsrfToken = pick("SNlM0e");
    return {
      xsrfToken: xsrfToken || null,
      geminiBl: geminiBl || null,
      source: xsrfToken || geminiBl ? "fetched-html" : null
    };
  } catch (error) {
    return { xsrfToken: null, geminiBl: null, source: null, error: error?.message || String(error) };
  }
}

async function resolvePageMetadata(tabs) {
  const fromTab = await readGeminiPageMetadata(tabs);
  if (fromTab.xsrfToken) return fromTab;

  const fromFetch = await fetchGeminiPageMetadata();
  if (fromFetch.xsrfToken) return fromFetch;

  return fromTab.error ? fromTab : fromFetch;
}

// ─── Session assembly ────────────────────────────────────────────────────────

async function buildInspection() {
  const [cookies, tabs] = await Promise.all([
    readGoogleCookies(),
    chrome.tabs.query({ url: "https://gemini.google.com/*" })
  ]);

  const selected = selectBestCookies(cookies);
  const validation = validateSelection(selected);
  const pageMetadata = await resolvePageMetadata(tabs);

  return {
    cookies,
    tabs,
    selected,
    validation,
    legacyPresent: listPresent(selected, LEGACY_COOKIES),
    legacyMissing: LEGACY_COOKIES.filter((name) => !selected.has(name)),
    available: summarizeAvailable(cookies),
    authUser: getActiveGeminiAccount(tabs),
    pageMetadata
  };
}

function inspectionMessage(info) {
  const {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  } = info;

  const lines = [
    `Found ${cookies.length} cookie(s) across Google domains.`,
    "",
    `SAPISID: ${selected.has("SAPISID") ? "present" : "missing"}`,
    `Session cookie: ${validation.sessionCookie || "missing"}`,
    `XSRF / SNlM0e: ${pageMetadata.xsrfToken ? "present" : "missing"}`,
    `gemini_bl / cfb2h: ${pageMetadata.geminiBl ? "present" : "missing"}`,
    `auth_user: ${authUser ?? "default account"}`,
    `Page data source: ${pageMetadata.source || "unavailable"}`,
    "",
    `Legacy present: ${legacyPresent.join(", ") || "none"}`,
    `Legacy not visible to the extension: ${legacyMissing.join(", ") || "none"}`,
    "",
    `Available for export: ${available.join(", ") || "none"}`
  ];

  if (pageMetadata.error) {
    lines.push("", `Page note: ${pageMetadata.error}`);
  }

  if (validation.valid && pageMetadata.xsrfToken) {
    lines.push("", "Session and XSRF are ready for export.");
  } else if (validation.valid) {
    lines.push("", "Cookies are ready, but XSRF is missing. Open Gemini, refresh the page, then inspect again.");
  } else {
    lines.push("", "Session is incomplete: SAPISID and one of __Secure-1PSID, __Secure-3PSID, or SID are required.");
  }

  return lines.join("\n");
}

function buildAuthPayload(info) {
  return {
    cookie: buildCookieString(info.selected),
    sapisid: info.selected.get("SAPISID").value,
    auth_user: info.authUser,
    xsrf_token: info.pageMetadata.xsrfToken,
    gemini_bl: info.pageMetadata.geminiBl
  };
}

/**
 * Collect the live session and build the sync payload.
 * Returns { payload, info } or { error }.
 */
async function collectSession({ requireXsrf = true } = {}) {
  const info = await buildInspection();

  if (!info.validation.valid) {
    return {
      error: "Session is incomplete: SAPISID and one of __Secure-1PSID, __Secure-3PSID, or SID are required."
    };
  }
  if (requireXsrf && !info.pageMetadata.xsrfToken) {
    return {
      error: "XSRF (SNlM0e) not found. Open gemini.google.com, sign in, and refresh, then retry."
    };
  }

  return { payload: buildAuthPayload(info), info };
}

// ─── Server push ─────────────────────────────────────────────────────────────

function syncEndpoint(settings) {
  const base = (settings.serverUrl || DEFAULT_SETTINGS.serverUrl).trim().replace(/\/+$/, "");
  return base + SYNC_PATH;
}

async function pushSessionToServer(settings, payload) {
  const headers = { "Content-Type": "application/json" };
  if (settings.syncToken) headers["X-Sync-Token"] = settings.syncToken;

  const response = await fetch(syncEndpoint(settings), {
    method: "POST",
    headers,
    body: JSON.stringify(payload)
  });

  const text = await response.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch {
    data = { raw: text };
  }

  if (!response.ok) {
    const message = data?.error?.message || data?.error || data?.raw || `HTTP ${response.status}`;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

/**
 * Full sync: collect the session and push it to the local server.
 * Returns a result object; never throws.
 */
async function syncSessionToServer({ settings = null } = {}) {
  const activeSettings = settings || (await getSettings());

  const session = await collectSession({ requireXsrf: true });
  if (session.error) {
    return { ok: false, error: session.error };
  }

  try {
    const result = await pushSessionToServer(activeSettings, session.payload);
    return {
      ok: true,
      result,
      info: session.info,
      payload: session.payload,
      endpoint: syncEndpoint(activeSettings)
    };
  } catch (error) {
    return {
      ok: false,
      error: error?.message || String(error),
      endpoint: syncEndpoint(activeSettings)
    };
  }
}

// ─── Local file download (manual fallback) ───────────────────────────────────

async function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"], {
    type: "application/json;charset=utf-8"
  });
  const url = URL.createObjectURL(blob);

  await chrome.downloads.download({
    url,
    filename,
    saveAs: true,
    conflictAction: "uniquify"
  });

  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
