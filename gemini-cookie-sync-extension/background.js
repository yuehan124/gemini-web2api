// background.js — periodically pushes the live Gemini session to the local
// gemini-web2api server so `gemini-auth.json` / `config.json` stay fresh.

importScripts("auth.js");

const ALARM_NAME = "gemini-cookie-sync";

async function scheduleAlarm() {
  await chrome.alarms.clear(ALARM_NAME);

  const settings = await getSettings();
  if (!settings.autoSync) return;

  const minutes = Math.max(1, Number(settings.intervalMinutes) || DEFAULT_SETTINGS.intervalMinutes);
  chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: 1,
    periodInMinutes: minutes
  });
}

/**
 * Run one sync. Returns the result object so callers (e.g. the popup) can show
 * details. Also records the outcome in chrome.storage.local.
 */
async function runSync(trigger = "auto") {
  let result;
  try {
    result = await syncSessionToServer();
  } catch (error) {
    result = { ok: false, error: error?.message || String(error) };
  }

  const stamp = new Date().toLocaleTimeString();
  const message = result.ok
    ? `Synced ${stamp} (${trigger})`
    : `Failed ${stamp} (${trigger}): ${result.error}`;

  await chrome.storage.local.set({
    lastSync: {
      at: Date.now(),
      status: result.ok ? "ok" : "error",
      message,
      trigger,
      detail: result.ok
        ? {
            endpoint: result.endpoint,
            auth_file: result.result?.auth_file || null,
            updates: Object.keys(result.result?.updates || {})
          }
        : { endpoint: result.endpoint || null }
    }
  });

  return result;
}

chrome.runtime.onInstalled.addListener(scheduleAlarm);
chrome.runtime.onStartup.addListener(scheduleAlarm);

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  // Only reschedule when the scheduling settings actually change; runSync also
  // writes to storage and must not reset the alarm period.
  if ("autoSync" in changes || "intervalMinutes" in changes) {
    scheduleAlarm();
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) runSync("auto");
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "sync-now") {
    runSync("manual").then(sendResponse);
    return true; // keep the message channel open for the async response
  }
  if (message?.type === "reschedule") {
    scheduleAlarm().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

// Make sure an alarm exists after the worker wakes for any reason.
scheduleAlarm();
