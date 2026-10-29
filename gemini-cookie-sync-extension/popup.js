// popup.js — UI wiring. Shared session logic lives in auth.js.

const statusEl = document.getElementById("status");
const syncButton = document.getElementById("sync");
const exportButton = document.getElementById("export");
const inspectButton = document.getElementById("inspect");
const openButton = document.getElementById("open");
const serverUrlInput = document.getElementById("serverUrl");
const syncTokenInput = document.getElementById("syncToken");
const intervalInput = document.getElementById("intervalMinutes");
const autoSyncInput = document.getElementById("autoSync");

function setStatus(message, kind = "") {
  statusEl.textContent = message;
  statusEl.className = kind;
}

function describeUpdates(updates) {
  const names = Array.isArray(updates) ? updates : Object.keys(updates || {});
  return names.length ? names.join(", ") : "none";
}

// ─── Settings ────────────────────────────────────────────────────────────────

async function loadSettingsIntoForm() {
  const settings = await getSettings();
  serverUrlInput.value = settings.serverUrl || "";
  syncTokenInput.value = settings.syncToken || "";
  intervalInput.value = settings.intervalMinutes || 5;
  autoSyncInput.checked = Boolean(settings.autoSync);
}

function readFormSettings() {
  return {
    serverUrl: serverUrlInput.value.trim() || DEFAULT_SETTINGS.serverUrl,
    syncToken: syncTokenInput.value.trim(),
    intervalMinutes: Math.max(1, Number(intervalInput.value) || DEFAULT_SETTINGS.intervalMinutes),
    autoSync: autoSyncInput.checked
  };
}

async function persistSettings() {
  const settings = readFormSettings();
  await saveSettings(settings);
  // Ask the worker to (re)create the alarm with the new interval.
  try {
    await chrome.runtime.sendMessage({ type: "reschedule" });
  } catch {
    // Worker may not be running yet; it schedules on install/startup anyway.
  }
  return settings;
}

for (const el of [serverUrlInput, syncTokenInput, intervalInput, autoSyncInput]) {
  el.addEventListener("change", persistSettings);
}

// ─── Last sync banner ────────────────────────────────────────────────────────

async function showLastSync() {
  const last = await getLastSync();
  if (!last) {
    setStatus("Ready.");
    return;
  }
  const when = last.at ? new Date(last.at).toLocaleString() : "unknown time";
  const extra = last.status === "ok" && last.detail?.auth_file
    ? `\n\nSaved to: ${last.detail.auth_file}\nConfig keys: ${describeUpdates(last.detail.updates)}`
    : "";
  setStatus(`${last.status === "ok" ? "Last sync OK" : "Last sync failed"} — ${when}\n${last.message}${extra}`,
    last.status === "ok" ? "ok" : "warn");
}

// ─── Buttons ─────────────────────────────────────────────────────────────────

openButton.addEventListener("click", async () => {
  await chrome.tabs.create({ url: "https://gemini.google.com/app" });
});

inspectButton.addEventListener("click", async () => {
  inspectButton.disabled = true;
  setStatus("Inspecting session and page data…");

  try {
    const info = await buildInspection();
    const ready = info.validation.valid && Boolean(info.pageMetadata.xsrfToken);
    setStatus(inspectionMessage(info), ready ? "ok" : "warn");
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  } finally {
    inspectButton.disabled = false;
  }
});

syncButton.addEventListener("click", async () => {
  syncButton.disabled = true;
  const settings = await persistSettings();
  setStatus(`Syncing to ${syncEndpoint(settings)} …`);

  try {
    const response = await chrome.runtime.sendMessage({ type: "sync-now" });

    if (!response || !response.ok) {
      throw new Error(response?.error || "sync failed");
    }

    const result = response.result || {};
    const authFile = result.auth_file || "(unknown)";
    const configFile = result.config_file || "(not updated)";
    const cookieCount = (response.payload?.cookie || "").split("; ").filter(Boolean).length;

    setStatus(
      `Synced to ${response.endpoint}\n\n` +
      `auth file:   ${authFile}\n` +
      `config file: ${configFile}\n` +
      `config keys updated: ${describeUpdates(result.updates)}\n` +
      `cookies sent: ${cookieCount}\n` +
      `XSRF: present\n` +
      `auth_user: ${response.payload?.auth_user ?? "null"}\n\n` +
      "The running server picked the session up without a restart.",
      "ok"
    );
  } catch (error) {
    setStatus(`Sync failed: ${error?.message || String(error)}`, "warn");
  } finally {
    syncButton.disabled = false;
  }
});

exportButton.addEventListener("click", async () => {
  exportButton.disabled = true;
  setStatus("Reading cookies and XSRF…");

  try {
    const info = await buildInspection();

    if (!info.validation.valid || !info.pageMetadata.xsrfToken) {
      throw new Error(inspectionMessage(info));
    }

    const payload = buildAuthPayload(info);
    await downloadJson("gemini-auth.json", payload);

    const exportedNames = EXPORT_ORDER.filter((name) => info.selected.has(name));
    setStatus(
      `Created gemini-auth.json with ${exportedNames.length} cookie(s) and XSRF.\n\n` +
      `Session cookie: ${info.validation.sessionCookie}\n` +
      `XSRF: present\n` +
      `gemini_bl: ${info.pageMetadata.geminiBl ? "present" : "not present — current server setting will remain"}\n` +
      `auth_user: ${info.authUser ?? "null"}\n\n` +
      "Move the file into gemini-web2api and do not share it or commit it to Git.",
      "ok"
    );
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  } finally {
    exportButton.disabled = false;
  }
});

// ─── Init ────────────────────────────────────────────────────────────────────

(async () => {
  try {
    await loadSettingsIntoForm();
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
    return;
  }
  await showLastSync();
})();
