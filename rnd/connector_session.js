/**
 * Keeps the local connector API (5055) running while this page is open.
 * Requires connector_watchdog.py on port 5056 (install once via install_ui_watchdog.ps1).
 */
(function (global) {
  "use strict";

  var PRESENCE_MS = 15000;

  function host() {
    return global.location && global.location.hostname
      ? global.location.hostname
      : "127.0.0.1";
  }

  function apiBase() {
    if (global.DATAHIVE_CONNECTOR_API) {
      return String(global.DATAHIVE_CONNECTOR_API).replace(/\/$/, "");
    }
    return "http://" + host() + ":5055";
  }

  function watchdogBase() {
    return "http://" + host() + ":5056";
  }

  function tabId() {
    var key = "datahive_tab_id";
    try {
      var id = sessionStorage.getItem(key);
      if (!id) {
        id =
          global.crypto && crypto.randomUUID
            ? crypto.randomUUID()
            : "tab-" + Date.now() + "-" + Math.random().toString(16).slice(2);
        sessionStorage.setItem(key, id);
      }
      return id;
    } catch (e) {
      return "tab-anonymous";
    }
  }

  function tabHeaders(extra) {
    var h = { "X-DataHive-Tab": tabId() };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        h[k] = extra[k];
      });
    }
    return h;
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  async function apiHealthy() {
    try {
      var res = await fetch(apiBase() + "/health", {
        headers: tabHeaders(),
        cache: "no-store",
      });
      if (!res.ok) return false;
      var data = await res.json();
      return !!(data && data.query_log_api === true);
    } catch (err) {
      return false;
    }
  }

  async function wakeWatchdog() {
    try {
      await fetch(watchdogBase() + "/ensure", {
        method: "POST",
        headers: tabHeaders(),
      });
    } catch (err) {
      return false;
    }
    for (var i = 0; i < 30; i++) {
      if (await apiHealthy()) return true;
      await sleep(400);
    }
    return apiHealthy();
  }

  function sendPresence() {
    fetch(watchdogBase() + "/presence", {
      method: "POST",
      headers: tabHeaders(),
      cache: "no-store",
    }).catch(function () {});
  }

  function sendRelease() {
    var url = watchdogBase() + "/release";
    var id = tabId();
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(url, id);
        return;
      }
    } catch (e) {
      /* ignore */
    }
    fetch(url, {
      method: "POST",
      headers: tabHeaders({ "Content-Type": "text/plain" }),
      body: id,
      keepalive: true,
    }).catch(function () {});
  }

  async function ensureConnectorApi() {
    if (await apiHealthy()) {
      sendPresence();
      return true;
    }
    var ok = await wakeWatchdog();
    if (ok) sendPresence();
    return ok;
  }

  global.ensureDataHiveConnectorApi = ensureConnectorApi;

  ensureConnectorApi().then(function (ok) {
    if (!ok) {
      console.warn(
        "[datahive] Local connector API unavailable. Start the UI via rnd/Open DataHive UI.bat " +
          "or run: pythonw rnd/connector_watchdog.py"
      );
      return;
    }
    sendPresence();
    setInterval(function () {
      if (document.hidden) return;
      sendPresence();
    }, PRESENCE_MS);
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) sendPresence();
  });

  global.addEventListener("pagehide", sendRelease);
  global.addEventListener("beforeunload", sendRelease);
})(window);
