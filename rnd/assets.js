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
    var text = await res.text();
    if (!res.ok) {
      var detail = text || "HTTP " + res.status;
      try {
        var parsed = JSON.parse(text);
        if (parsed && parsed.detail) {
          detail =
            typeof parsed.detail === "string"
              ? parsed.detail
              : JSON.stringify(parsed.detail);
        }
      } catch (_e) {
        /* keep raw text */
      }
      if (res.status === 404) {
        detail =
          "Connector API route not found (404). Restart connector_api.py from the rnd folder.";
      }
      var err = new Error(detail);
      err.httpStatus = res.status;
      throw err;
    }
    return text ? JSON.parse(text) : {};
  }

  async function summary() {
    try {
      return await fetchJson("/api/assets/schemas");
    } catch (err) {
      if (err && err.httpStatus === 404) {
        return fetchJson("/api/assets/counts");
      }
      throw err;
    }
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
    },
    summary: summary,
    schemas: function () {
      return summary();
    },
    tables: function (schema) {
      return fetchJson("/api/assets/tables?schema=" + encodeURIComponent(schema));
    },
    structure: function (schema, table) {
      return fetchJson(
        "/api/assets/structure?schema=" +
          encodeURIComponent(schema) +
          "&table=" +
          encodeURIComponent(table)
      );
    }
  };
})(window);
