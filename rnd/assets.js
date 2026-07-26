/**
 * Assets search & discovery — PostgreSQL via connector API (POSTGRES_* in repo `.env`).
 */
(function (global) {
  "use strict";

  function apiBase() {
    if (global.DATAHIVE_CONNECTOR_API) {
      return String(global.DATAHIVE_CONNECTOR_API).replace(/\/$/, "");
    }
    var host = "127.0.0.1";
    if (global.location && global.location.hostname) {
      host = global.location.hostname;
    }
    return "http://" + host + ":5055";
  }

  function userHeader() {
    var el = document.getElementById("userNm");
    var name = el && el.textContent ? el.textContent.trim() : "";
    return name || "Admin";
  }

  function headers() {
    return { "X-DataHive-User": userHeader() };
  }

  async function fetchJson(path) {
    var res = await fetch(apiBase() + path, { headers: headers() });
    if (!res.ok) {
      var text = await res.text();
      throw new Error(text || "HTTP " + res.status);
    }
    return res.json();
  }

  global.DataHiveAssets = {
    relevant: function (tab, type) {
      var q = new URLSearchParams({ tab: tab || "recently_verified" });
      if (type) q.set("type", type);
      return fetchJson("/api/assets/relevant?" + q.toString());
    },
    search: function (query) {
      return fetchJson(
        "/api/assets/search?q=" + encodeURIComponent(query || "")
      );
    },
    discover: function () {
      return fetchJson("/api/assets/discover?limit=200");
    }
  };
})(window);
