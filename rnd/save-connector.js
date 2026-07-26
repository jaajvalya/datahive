/**
 * Saves connector connection attributes to MongoDB (database from MONGO_URI in `.env`),
 * collection `connectors`. All connection attempts are logged to `connection_logs`.
 *
 * Requires the companion API: `python connector_api.py` (port 5055).
 * Attached from main.html and invoked on "Connect & fetch".
 */
(function (global) {
  "use strict";

  function connectorApiBase() {
    if (global.DATAHIVE_CONNECTOR_API) {
      return String(global.DATAHIVE_CONNECTOR_API).replace(/\/$/, "");
    }
    var host =
      global.location && global.location.hostname
        ? global.location.hostname
        : "127.0.0.1";
    return "http://" + host + ":5055";
  }

  var API_URL = connectorApiBase() + "/api/connectors";
  var UPLOAD_API_URL = connectorApiBase() + "/api/connectors/upload";
  var CONNECTION_LOGS_URL = connectorApiBase() + "/api/connection-logs";
  var HEALTH_URL = connectorApiBase() + "/health";

  var SENSITIVE_KEYS = {
    api_key: true,
    client_secret: true,
    secret_access_key: true,
    service_account_json: true
  };

  function getRequestUser() {
    var el = document.getElementById("userNm");
    if (el && el.textContent) {
      var name = el.textContent.trim();
      if (name) return name;
    }
    return "unknown";
  }

  function requestHeaders(extra) {
    var headers = { "X-DataHive-User": getRequestUser() };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        headers[k] = extra[k];
      });
    }
    return headers;
  }

  function sanitizeContext(obj) {
    if (obj == null || typeof obj !== "object") return obj;
    if (Array.isArray(obj)) {
      return obj.map(function (item) {
        return typeof item === "object" ? sanitizeContext(item) : item;
      });
    }
    var out = {};
    Object.keys(obj).forEach(function (key) {
      if (SENSITIVE_KEYS[key]) {
        out[key] = "[redacted]";
      } else if (obj[key] && typeof obj[key] === "object") {
        out[key] = sanitizeContext(obj[key]);
      } else {
        out[key] = obj[key];
      }
    });
    return out;
  }

  /**
   * Persist a connection log record (success or failure) to MongoDB connection_logs.
   */
  function logConnectionEvent(details) {
    if (!details || !details.message) return;
    var outcome = details.outcome === "success" ? "success" : "failure";
    var body = {
      user: getRequestUser(),
      message: String(details.message),
      event:
        details.event ||
        (outcome === "success" ? "connection.saved" : "connection.error"),
      outcome: outcome,
      error_type: outcome === "success" ? null : details.error_type || "client",
      context: sanitizeContext(details.context || {})
    };
    fetch(CONNECTION_LOGS_URL, {
      method: "POST",
      headers: requestHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body)
    }).catch(function (err) {
      console.warn("[save-connector] could not write connection_logs", err);
    });
  }

  function logConnectionFailure(details) {
    logConnectionEvent(
      Object.assign({}, details, { outcome: "failure" })
    );
  }

  function logConnectionSuccess(details) {
    logConnectionEvent(
      Object.assign({}, details, { outcome: "success", error_type: null })
    );
  }

  /**
   * Build the document persisted in the connectors collection.
   * Includes connection attributes and all form input details.
   */
  function buildConnectorDocument(payload) {
    return {
      user: getRequestUser(),
      connector_type: payload.connector_type || null,
      cloud: payload.cloud || null,
      mode: payload.mode || "cloud",
      display_name: payload.display_name || null,
      account_id: payload.account_id || null,
      region: payload.region || null,
      auth_type: payload.auth_type || null,
      apis: Array.isArray(payload.apis) ? payload.apis.slice() : [],
      dataset_scope: payload.dataset_scope || null,
      tenant_id: payload.tenant_id || null,
      resource_group: payload.resource_group || null,
      api_key: payload.api_key || null,
      client_id: payload.client_id || null,
      client_secret: payload.client_secret || null,
      service_account_json: payload.service_account_json || null,
      access_key_id: payload.access_key_id || null,
      secret_access_key: payload.secret_access_key || null,
      role_arn: payload.role_arn || null,
      file_name: payload.file_name || null,
      file_size: payload.file_size != null ? payload.file_size : null,
      file_type: payload.file_type || null,
      upload_format: payload.upload_format || null,
      upload_notes: payload.upload_notes || null,
      saved_at: new Date().toISOString()
    };
  }

  /**
   * Persist connection attributes + input details to MongoDB via the local API.
   * For file-upload mode, also stores the file under rnd/UPLOAD on the server.
   * @param {object} payload - Form payload built by main.html on submit
   * @param {File|null} uploadFile - Selected file when mode is upload
   * @returns {Promise<{ok:boolean,id:string}>}
   */
  async function saveConnectorToMongo(payload, uploadFile) {
    if (!payload || typeof payload !== "object") {
      throw new Error("Missing connector payload.");
    }

    var document = buildConnectorDocument(payload);
    var isUpload =
      payload.mode === "upload" ||
      payload.cloud === "upload" ||
      payload.auth_type === "file_upload";
    var res;

    try {
      if (isUpload) {
        if (!uploadFile) {
          throw new Error("Missing upload file.");
        }
        var form = new FormData();
        form.append("file", uploadFile, uploadFile.name);
        form.append("metadata", JSON.stringify(document));
        res = await fetch(UPLOAD_API_URL, {
          method: "POST",
          headers: requestHeaders(),
          body: form
        });
      } else {
        res = await fetch(API_URL, {
          method: "POST",
          headers: requestHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify(document)
        });
      }
    } catch (networkErr) {
      logConnectionFailure({
        message: networkErr && networkErr.message ? networkErr.message : String(networkErr),
        event: "connection.save_failed",
        error_type: "network",
        context: {
          cloud: payload.cloud,
          mode: payload.mode,
          display_name: payload.display_name,
          connector_type: payload.connector_type
        }
      });
      throw networkErr;
    }

    var bodyText = await res.text();
    var data = null;
    try {
      data = bodyText ? JSON.parse(bodyText) : null;
    } catch {
      data = { raw: bodyText };
    }

    if (!res.ok) {
      var msg =
        (data && (data.detail || data.error || data.message)) ||
        bodyText ||
        "HTTP " + res.status;
      var errText = typeof msg === "string" ? msg : JSON.stringify(msg);
      throw new Error(errText);
    }

    console.info("[save-connector] saved to connectors collection", data);
    return data;
  }

  /**
   * Fetch the most recently saved connectors from MongoDB.
   * @param {number} limit
   * @returns {Promise<{ok:boolean,items:object[]}>}
   */
  async function fetchRecentConnectors(limit) {
    var url =
      connectorApiBase() +
      "/api/connectors/recent?limit=" +
      encodeURIComponent(limit == null ? 3 : limit);
    var res = await fetch(url, { headers: requestHeaders() });
    var bodyText = await res.text();
    var data = null;
    try {
      data = bodyText ? JSON.parse(bodyText) : null;
    } catch {
      data = { raw: bodyText };
    }
    if (!res.ok) {
      var msg =
        (data && (data.detail || data.error || data.message)) ||
        bodyText ||
        "HTTP " + res.status;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return data;
  }

  global.buildConnectorDocument = buildConnectorDocument;
  global.saveConnectorToMongo = saveConnectorToMongo;
  global.fetchRecentConnectors = fetchRecentConnectors;
  global.logConnectionFailure = logConnectionFailure;
  global.logConnectionSuccess = logConnectionSuccess;
  global.logConnectionEvent = logConnectionEvent;
  global.getDataHiveUser = getRequestUser;
  global.checkConnectorApiHealth = async function checkConnectorApiHealth() {
    try {
      var res = await fetch(HEALTH_URL, { headers: requestHeaders() });
      if (!res.ok) return { ok: false, detail: "HTTP " + res.status };
      return await res.json();
    } catch (err) {
      return {
        ok: false,
        detail: err && err.message ? err.message : String(err)
      };
    }
  };
})(window);
