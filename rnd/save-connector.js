/**
 * Saves connector connection attributes to the local MongoDB
 * database `datahivepoc`, collection `connectors`.
 *
 * Requires the companion API: `python connector_api.py` (port 5055).
 * Attached from main.html and invoked on "Connect & fetch".
 */
(function (global) {
  "use strict";

  var API_URL = "http://127.0.0.1:5055/api/connectors";

  /**
   * Build the document persisted in `datahivepoc.connectors`.
   * Includes connection attributes and all form input details.
   */
  function buildConnectorDocument(payload) {
    return {
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
      // Credential / auth input details (as entered on the form)
      api_key: payload.api_key || null,
      client_id: payload.client_id || null,
      client_secret: payload.client_secret || null,
      service_account_json: payload.service_account_json || null,
      access_key_id: payload.access_key_id || null,
      secret_access_key: payload.secret_access_key || null,
      role_arn: payload.role_arn || null,
      // Upload mode fields
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
   * @param {object} payload - Form payload built by main.html on submit
   * @returns {Promise<{ok:boolean,id:string}>}
   */
  async function saveConnectorToMongo(payload) {
    if (!payload || typeof payload !== "object") {
      throw new Error("Missing connector payload.");
    }

    var document = buildConnectorDocument(payload);
    var res = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(document)
    });

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

    console.info("[save-connector] saved to datahivepoc.connectors", data);
    return data;
  }

  global.buildConnectorDocument = buildConnectorDocument;
  global.saveConnectorToMongo = saveConnectorToMongo;
})(window);
