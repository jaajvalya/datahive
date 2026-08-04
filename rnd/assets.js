/**
 * Assets search & discovery — multi-connector catalog via connector API.
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

  function roleHeader() {
    if (typeof global.getDataHiveUserRole === "function") {
      return global.getDataHiveUserRole() || "admin";
    }
    // Default Admin chip → full access until a signed-in user model exists.
    var name = userHeader().toLowerCase();
    if (name === "admin" || name === "administrator") return "admin";
    return "editor";
  }

  function headers() {
    return {
      "X-DataHive-User": userHeader(),
      "X-DataHive-Role": roleHeader(),
    };
  }

  function withConnector(path, connectorId) {
    if (!connectorId) return path;
    var sep = path.indexOf("?") >= 0 ? "&" : "?";
    return path + sep + "connector_id=" + encodeURIComponent(connectorId);
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

  async function summary(connectorId) {
    try {
      return await fetchJson(withConnector("/api/assets/schemas", connectorId || "all"));
    } catch (err) {
      if (err && err.httpStatus === 404) {
        return fetchJson(withConnector("/api/assets/counts", connectorId || "all"));
      }
      throw err;
    }
  }

  global.DataHiveAssets = {
    connectors: function () {
      return fetchJson("/api/assets/connectors");
    },
    catalog: function (connectorId) {
      return fetchJson(withConnector("/api/assets/catalog", connectorId || "all"));
    },
    relevant: function (tab, type, connectorId) {
      var q = new URLSearchParams({ tab: tab || "recently_verified" });
      if (type) q.set("type", type);
      if (connectorId) q.set("connector_id", connectorId);
      return fetchJson("/api/assets/relevant?" + q.toString());
    },
    search: function (query, connectorId) {
      var q = new URLSearchParams({ q: query || "" });
      if (connectorId) q.set("connector_id", connectorId);
      return fetchJson("/api/assets/search?" + q.toString());
    },
    discover: function (connectorId) {
      return fetchJson(withConnector("/api/assets/discover?limit=200", connectorId || "all"));
    },
    summary: summary,
    schemas: function (connectorId) {
      return summary(connectorId);
    },
    tables: function (schema, connectorId) {
      return fetchJson(
        withConnector(
          "/api/assets/tables?schema=" + encodeURIComponent(schema),
          connectorId
        )
      );
    },
    structure: function (schema, table, connectorId) {
      return fetchJson(
        withConnector(
          "/api/assets/structure?schema=" +
            encodeURIComponent(schema) +
            "&table=" +
            encodeURIComponent(table),
          connectorId
        )
      );
    },
  };
})(window);
