/**
 * ACL Digital–branded Reporting dashboards.
 * Business KPIs (Customer 360, sales trend, delivery channel) scoped by
 * connection → database → schema, with in-visual slice/dice.
 */
(function (global) {
  "use strict";

  var ACL = {
    navy: "#0056A7",
    navyDeep: "#182978",
    blue: "#046BD2",
    orange: "#FF671F",
    teal: "#1FA971",
    soft: "#3375B3",
    gray: "#8a93a3",
  };

  var PALETTE = [ACL.orange, ACL.navy, ACL.blue, ACL.teal, ACL.soft, "#F49121", "#00ADEF"];

  var SLICE_DEFS = [
    { id: "channel", label: "Delivery channel", barTitle: "Mode of delivery", barHint: "Preferred channel / fulfillment mix" },
    { id: "state", label: "State", barTitle: "Customers by state", barHint: "Geographic concentration" },
    { id: "tier", label: "Loyalty tier", barTitle: "Membership tiers", barHint: "Loyalty program mix" },
    { id: "payment", label: "Payment method", barTitle: "Payment mix", barHint: "Account transaction methods" },
  ];

  var state = {
    initialized: false,
    connectors: [],
    catalogItems: [],
    schemas: [],
    tables: {},
    kpis: null,
    activeChip: "all",
    scope: { connectorId: "", database: "", schema: "", slice: "channel", platform: "" },
  };

  function $(sel) {
    return document.querySelector(sel);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function apiBase() {
    if (global.DATAHIVE_CONNECTOR_API) {
      return String(global.DATAHIVE_CONNECTOR_API).replace(/\/$/, "");
    }
    var host = "127.0.0.1";
    if (global.location && global.location.hostname) host = global.location.hostname;
    return "http://" + host + ":5055";
  }

  function userHeader() {
    var el = document.getElementById("userNm");
    return (el && el.textContent && el.textContent.trim()) || "Admin";
  }

  function setStatus(msg) {
    var el = $("#rptStatus");
    if (el) el.textContent = msg || "";
  }

  function setError(msg) {
    var el = $("#rptError");
    if (!el) return;
    if (!msg) {
      el.classList.add("hidden");
      el.textContent = "";
      return;
    }
    el.textContent = msg;
    el.classList.remove("hidden");
  }

  function layerOf(schema) {
    var s = String(schema || "").toLowerCase();
    if (s.indexOf("bronze") >= 0) return "bronze";
    if (s.indexOf("silver") >= 0) return "silver";
    if (s.indexOf("gold") >= 0 || s.indexOf("mart") >= 0) return "gold";
    if (s.indexOf("raw") >= 0) return "raw";
    return "other";
  }

  function fmtNum(n, digits) {
    if (n == null || isNaN(n)) return "—";
    var d = digits == null ? 0 : digits;
    return Number(n).toLocaleString(undefined, {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function fmtMoney(n) {
    if (n == null || isNaN(n)) return "—";
    return "$" + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function prettyLabel(s) {
    return String(s || "—")
      .replace(/_/g, " ")
      .replace(/\b\w/g, function (c) {
        return c.toUpperCase();
      });
  }

  function quoteIdent(name, platform) {
    var n = String(name || "").replace(/"/g, '""');
    if (platform === "postgres") return '"' + n + '"';
    return '"' + n.toUpperCase().replace(/"/g, '""') + '"';
  }

  function qualifyTable(tableName, schema, platform) {
    var parts = [];
    var sch = String(schema || "");
    if (platform === "snowflake" && sch.indexOf(".") >= 0) {
      sch.split(".").forEach(function (p) {
        parts.push(quoteIdent(p, platform));
      });
    } else if (sch) {
      parts.push(quoteIdent(sch, platform));
    }
    parts.push(quoteIdent(tableName, platform));
    return parts.join(".");
  }

  function findTable(patterns) {
    var names = Object.keys(state.tables || {});
    for (var i = 0; i < patterns.length; i++) {
      var re = patterns[i];
      for (var j = 0; j < names.length; j++) {
        if (re.test(names[j])) return names[j];
      }
    }
    return null;
  }

  function schemaMatches(assetSchema, selectedSchema) {
    var a = String(assetSchema || "").toLowerCase();
    var s = String(selectedSchema || "").toLowerCase();
    if (!a || !s) return false;
    if (a === s) return true;
    var aShort = a.indexOf(".") >= 0 ? a.split(".").pop() : a;
    var sShort = s.indexOf(".") >= 0 ? s.split(".").pop() : s;
    if (aShort && aShort === sShort) return true;
    if (a.endsWith("." + s) || s.endsWith("." + a)) return true;
    return false;
  }

  function resolveTables() {
    var items = state.catalogItems || [];
    var schema = String(state.scope.schema || "");
    var map = {};
    items.forEach(function (a) {
      if (!a || !a.name) return;
      if (String(a.type || "") === "Schema") return;
      if (!schemaMatches(a.schema, schema)) return;
      map[String(a.name).toUpperCase()] = a.name;
    });
    state.tables = map;
    return {
      customer: findTable([/CUSTOMER/, /CUTOMER/]),
      accounts: findTable([/ACCOUNT/]),
      orderItems: findTable([/ORDER_ITEM/, /ORDERITEM/]),
      memberships: findTable([/MEMBER/]),
      stores: findTable([/STORE/]),
      products: findTable([/PRODUCT/]),
    };
  }

  async function runSql(sql) {
    var res = await fetch(apiBase() + "/api/sql/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-DataHive-User": userHeader(),
      },
      body: JSON.stringify({
        sql: sql,
        max_rows: 100,
        connector_id: state.scope.connectorId,
        schema: state.scope.schema,
      }),
    });
    var data = await res.json().catch(function () {
      return {};
    });
    if (!res.ok) {
      var detail = data.detail || data.message || "Query failed";
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function colIndex(columns, names) {
    var cols = (columns || []).map(function (c) {
      return String(c).toUpperCase();
    });
    for (var i = 0; i < names.length; i++) {
      var idx = cols.indexOf(String(names[i]).toUpperCase());
      if (idx >= 0) return idx;
    }
    return -1;
  }

  function rowsToPairs(result, keyNames, valueNames) {
    if (!result || !result.rows) return [];
    var ki = colIndex(result.columns, keyNames);
    var vi = colIndex(result.columns, valueNames);
    if (ki < 0 || vi < 0) return [];
    return result.rows
      .map(function (r) {
        return { key: String(r[ki] == null ? "Unknown" : r[ki]), value: Number(r[vi]) || 0 };
      })
      .filter(function (x) {
        return x.key;
      });
  }

  function rowObj(result, row) {
    var o = {};
    (result.columns || []).forEach(function (c, i) {
      o[String(c).toUpperCase()] = row[i];
    });
    return o;
  }

  async function loadConnectors() {
    var sel = $("#rptConnection");
    if (!sel) return;
    sel.innerHTML = '<option value="">Loading connections…</option>';
    try {
      if (typeof global.DataHiveAssets === "undefined") throw new Error("Assets API not loaded");
      var data = await global.DataHiveAssets.connectors();
      state.connectors = data.items || [];
    } catch (_err) {
      state.connectors = [
        { id: "local-postgres", display_name: "Local Postgres", platform: "postgres" },
      ];
    }
    sel.innerHTML =
      '<option value="">Select connection…</option>' +
      state.connectors
        .map(function (c) {
          var label =
            (c.display_name || c.id) +
            (c.platform || c.cloud ? " · " + (c.platform || c.cloud) : "");
          return (
            '<option value="' +
            escapeHtml(c.id) +
            '">' +
            escapeHtml(label) +
            "</option>"
          );
        })
        .join("");
  }

  function resetDownstream(level) {
    var db = $("#rptDatabase");
    var sch = $("#rptSchema");
    var apply = $("#rptApplyBtn");
    if (level === "connection") {
      if (db) {
        db.disabled = true;
        db.innerHTML = '<option value="">Select connection first…</option>';
      }
      if (sch) {
        sch.disabled = true;
        sch.innerHTML = '<option value="">Select database first…</option>';
      }
      if (apply) apply.disabled = true;
    } else if (level === "database") {
      if (sch) {
        sch.disabled = true;
        sch.innerHTML = '<option value="">Select database first…</option>';
      }
      if (apply) apply.disabled = true;
    }
  }

  async function onConnectionChange() {
    var connectorId = ($("#rptConnection") && $("#rptConnection").value) || "";
    setError("");
    resetDownstream("connection");
    hideDashboard();
    if (!connectorId) return;
    setStatus("Loading catalog…");
    try {
      if (typeof global.DataHiveAssets === "undefined") throw new Error("Assets API not loaded");
      var catalog = await global.DataHiveAssets.catalog(connectorId);
      state.catalogItems = catalog.items || [];
      state.schemas = catalog.schemas || [
        ...new Set(
          state.catalogItems
            .map(function (i) {
              return i.schema;
            })
            .filter(Boolean)
        ),
      ];
      var dbs = new Set();
      state.catalogItems.forEach(function (a) {
        if (a.database) dbs.add(String(a.database));
      });
      var conn = state.connectors.find(function (c) {
        return c.id === connectorId;
      });
      if (conn && conn.dataset_scope) {
        dbs.add(String(conn.dataset_scope).split(/[,;\n]/)[0].trim());
      }
      state.schemas.forEach(function (s) {
        var layer = layerOf(s);
        if (layer !== "other") dbs.add(layer);
        if (String(s).indexOf(".") >= 0) dbs.add(String(s).split(".")[0]);
      });
      if (!dbs.size) dbs.add("default");
      var dbSel = $("#rptDatabase");
      if (dbSel) {
        dbSel.disabled = false;
        dbSel.innerHTML =
          '<option value="">Select database…</option>' +
          [...dbs]
            .filter(Boolean)
            .sort()
            .map(function (d) {
              return '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + "</option>";
            })
            .join("");
      }
      setStatus("Select database and schema.");
    } catch (err) {
      setError(err && err.message ? err.message : String(err));
      setStatus("");
    }
  }

  function onDatabaseChange() {
    var database = ($("#rptDatabase") && $("#rptDatabase").value) || "";
    var sch = $("#rptSchema");
    var apply = $("#rptApplyBtn");
    resetDownstream("database");
    hideDashboard();
    if (!database || !sch) return;
    var db = database.toLowerCase();
    var list = state.schemas.filter(Boolean);
    var filtered = list.filter(function (s) {
      var sl = String(s).toLowerCase();
      if (["bronze", "silver", "gold", "raw"].indexOf(db) >= 0) return sl.indexOf(db) >= 0;
      if (sl.indexOf(db + ".") === 0) return true;
      return state.catalogItems.some(function (a) {
        return a.schema === s && String(a.database || "").toLowerCase() === db;
      });
    });
    if (!filtered.length) filtered = list;
    sch.disabled = false;
    sch.innerHTML =
      '<option value="">Select schema…</option>' +
      filtered
        .map(function (s) {
          return '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + "</option>";
        })
        .join("");
    if (apply) apply.disabled = true;
    setStatus("Select a schema, then apply.");
  }

  function onSchemaChange() {
    var schema = ($("#rptSchema") && $("#rptSchema").value) || "";
    var apply = $("#rptApplyBtn");
    if (apply) apply.disabled = !schema;
    hideDashboard();
    if (schema) setStatus("Ready — click Apply dashboard.");
  }

  function hideDashboard() {
    var empty = $("#rptEmpty");
    var dash = $("#rptDashboard");
    if (empty) empty.classList.remove("hidden");
    if (dash) dash.classList.add("hidden");
    state.activeChip = "all";
    state.kpis = null;
  }

  function showDashboard() {
    var empty = $("#rptEmpty");
    var dash = $("#rptDashboard");
    if (empty) empty.classList.add("hidden");
    if (dash) dash.classList.remove("hidden");
  }

  function channelWhere(platform) {
    if (state.scope.slice !== "channel" || state.activeChip === "all") return "";
    var col = platform === "postgres" ? "preferred_channel" : "PREFERRED_CHANNEL";
    return " WHERE " + quoteIdent(col, platform) + " = '" + String(state.activeChip).replace(/'/g, "''") + "'";
  }

  function stateWhere(platform) {
    if (state.scope.slice !== "state" || state.activeChip === "all") return "";
    var col = platform === "postgres" ? "state" : "STATE";
    return " WHERE " + quoteIdent(col, platform) + " = '" + String(state.activeChip).replace(/'/g, "''") + "'";
  }

  function customerFilter(platform) {
    return channelWhere(platform) || stateWhere(platform) || "";
  }

  async function fetchBusinessKpis(tables) {
    var platform = state.scope.platform;
    var schema = state.scope.schema;
    var q = function (t) {
      return qualifyTable(t, schema, platform);
    };
    var out = {
      available: [],
      headline: {},
      channel: [],
      state: [],
      tier: [],
      payment: [],
      salesTrend: [],
      products: [],
      customers: [],
      c360: {},
    };

    var custFilter = customerFilter(platform);

    if (tables.customer) {
      out.available.push("customer");
      try {
        var c360 = await runSql(
          "SELECT COUNT(*) AS customers, COUNT(DISTINCT " +
            quoteIdent(platform === "postgres" ? "city" : "CITY", platform) +
            ") AS cities, COUNT(DISTINCT " +
            quoteIdent(platform === "postgres" ? "state" : "STATE", platform) +
            ") AS states, COUNT(DISTINCT " +
            quoteIdent(platform === "postgres" ? "preferred_channel" : "PREFERRED_CHANNEL", platform) +
            ") AS channels FROM " +
            q(tables.customer) +
            custFilter
        );
        var r = c360.rows && c360.rows[0];
        if (r) {
          out.headline.customers = Number(r[0]) || 0;
          out.c360.cities = Number(r[1]) || 0;
          out.c360.states = Number(r[2]) || 0;
          out.c360.channels = Number(r[3]) || 0;
        }
      } catch (_e) {
        /* soft */
      }

      try {
        out.channel = rowsToPairs(
          await runSql(
            "SELECT " +
              quoteIdent(platform === "postgres" ? "preferred_channel" : "PREFERRED_CHANNEL", platform) +
              " AS channel, COUNT(*) AS n FROM " +
              q(tables.customer) +
              (stateWhere(platform) || "") +
              " GROUP BY 1 ORDER BY 2 DESC"
          ),
          ["CHANNEL"],
          ["N"]
        );
      } catch (_e2) {
        /* soft */
      }

      try {
        out.state = rowsToPairs(
          await runSql(
            "SELECT " +
              quoteIdent(platform === "postgres" ? "state" : "STATE", platform) +
              " AS st, COUNT(*) AS n FROM " +
              q(tables.customer) +
              (channelWhere(platform) || "") +
              " GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
          ),
          ["ST"],
          ["N"]
        );
      } catch (_e3) {
        /* soft */
      }

      try {
        var sample = await runSql(
          "SELECT " +
            [
              quoteIdent(platform === "postgres" ? "customer_id" : "CUSTOMER_ID", platform),
              quoteIdent(platform === "postgres" ? "first_name" : "FIRST_NAME", platform),
              quoteIdent(platform === "postgres" ? "last_name" : "LAST_NAME", platform),
              quoteIdent(platform === "postgres" ? "city" : "CITY", platform),
              quoteIdent(platform === "postgres" ? "state" : "STATE", platform),
              quoteIdent(platform === "postgres" ? "preferred_channel" : "PREFERRED_CHANNEL", platform),
            ].join(", ") +
            " FROM " +
            q(tables.customer) +
            custFilter +
            " LIMIT 20"
        );
        out.customers = (sample.rows || []).map(function (row) {
          return rowObj(sample, row);
        });
      } catch (_e4) {
        /* soft */
      }
    }

    if (tables.accounts) {
      out.available.push("accounts");
      try {
        var sales = await runSql(
          "SELECT TO_VARCHAR(DATE_TRUNC('month', TRY_TO_TIMESTAMP(TO_VARCHAR(" +
            quoteIdent("TXN_TS", platform) +
            "))), 'YYYY-MM') AS ym, SUM(TRY_TO_DOUBLE(TO_VARCHAR(" +
            quoteIdent("AMOUNT", platform) +
            "))) AS revenue, COUNT(*) AS txns FROM " +
            q(tables.accounts) +
            " WHERE TRY_TO_TIMESTAMP(TO_VARCHAR(" +
            quoteIdent("TXN_TS", platform) +
            ")) IS NOT NULL GROUP BY 1 ORDER BY 1"
        );
        out.salesTrend = (sales.rows || []).map(function (row) {
          var o = rowObj(sales, row);
          return {
            key: String(o.YM || ""),
            value: Number(o.REVENUE) || 0,
            txns: Number(o.TXNS) || 0,
          };
        });
        out.headline.revenue = out.salesTrend.reduce(function (s, x) {
          return s + x.value;
        }, 0);
        out.headline.txns = out.salesTrend.reduce(function (s, x) {
          return s + x.txns;
        }, 0);
      } catch (_pgSales) {
        // Postgres-friendly fallback
        try {
          var salesPg = await runSql(
            "SELECT to_char(date_trunc('month', " +
              quoteIdent("txn_ts", "postgres") +
              "::timestamp), 'YYYY-MM') AS ym, SUM((" +
              quoteIdent("amount", "postgres") +
              ")::numeric) AS revenue, COUNT(*) AS txns FROM " +
              q(tables.accounts) +
              " GROUP BY 1 ORDER BY 1"
          );
          out.salesTrend = (salesPg.rows || []).map(function (row) {
            var o = rowObj(salesPg, row);
            return {
              key: String(o.YM || ""),
              value: Number(o.REVENUE) || 0,
              txns: Number(o.TXNS) || 0,
            };
          });
          out.headline.revenue = out.salesTrend.reduce(function (s, x) {
            return s + x.value;
          }, 0);
          out.headline.txns = out.salesTrend.reduce(function (s, x) {
            return s + x.txns;
          }, 0);
        } catch (_e5) {
          /* soft */
        }
      }

      try {
        out.payment = rowsToPairs(
          await runSql(
            "SELECT " +
              quoteIdent(platform === "postgres" ? "payment_method" : "PAYMENT_METHOD", platform) +
              " AS mode, COUNT(*) AS n FROM " +
              q(tables.accounts) +
              " GROUP BY 1 ORDER BY 2 DESC"
          ),
          ["MODE"],
          ["N"]
        );
      } catch (_e6) {
        /* soft */
      }
    }

    if (tables.memberships) {
      out.available.push("memberships");
      try {
        var mem = await runSql("SELECT COUNT(*) AS members FROM " + q(tables.memberships));
        out.headline.members = Number(mem.rows && mem.rows[0] && mem.rows[0][0]) || 0;
      } catch (_e7) {
        /* soft */
      }
      try {
        out.tier = rowsToPairs(
          await runSql(
            "SELECT COALESCE(" +
              quoteIdent(platform === "postgres" ? "tier_name" : "TIER_NAME", platform) +
              ", " +
              quoteIdent(platform === "postgres" ? "tier_code" : "TIER_CODE", platform) +
              ", 'Unknown') AS tier, COUNT(*) AS n FROM " +
              q(tables.memberships) +
              " GROUP BY 1 ORDER BY 2 DESC"
          ),
          ["TIER"],
          ["N"]
        );
      } catch (_e8) {
        /* soft */
      }
    }

    if (tables.orderItems) {
      out.available.push("order_items");
      try {
        var oi = await runSql(
          "SELECT COUNT(*) AS lines, COUNT(DISTINCT " +
            quoteIdent(platform === "postgres" ? "order_id" : "ORDER_ID", platform) +
            ") AS orders, SUM(TRY_TO_DOUBLE(TO_VARCHAR(" +
            quoteIdent(platform === "postgres" ? "line_total" : "LINE_TOTAL", platform) +
            "))) AS revenue FROM " +
            q(tables.orderItems)
        );
        var oir = oi.rows && oi.rows[0];
        if (oir) {
          out.headline.orders = Number(oir[1]) || 0;
          if (out.headline.revenue == null) out.headline.revenue = Number(oir[2]) || 0;
        }
      } catch (_oiPg) {
        try {
          var oi2 = await runSql(
            "SELECT COUNT(*) AS lines, COUNT(DISTINCT " +
              quoteIdent("order_id", "postgres") +
              ") AS orders, SUM((" +
              quoteIdent("line_total", "postgres") +
              ")::numeric) AS revenue FROM " +
              q(tables.orderItems)
          );
          var oir2 = oi2.rows && oi2.rows[0];
          if (oir2) {
            out.headline.orders = Number(oir2[1]) || 0;
            if (out.headline.revenue == null) out.headline.revenue = Number(oir2[2]) || 0;
          }
        } catch (_e9) {
          /* soft */
        }
      }

      try {
        var top = await runSql(
          "SELECT " +
            quoteIdent(platform === "postgres" ? "product_name" : "PRODUCT_NAME", platform) +
            " AS product_name, SUM(TRY_TO_DOUBLE(TO_VARCHAR(" +
            quoteIdent(platform === "postgres" ? "line_total" : "LINE_TOTAL", platform) +
            "))) AS rev, SUM(TRY_TO_DOUBLE(TO_VARCHAR(" +
            quoteIdent(platform === "postgres" ? "quantity" : "QUANTITY", platform) +
            "))) AS qty FROM " +
            q(tables.orderItems) +
            " GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 8"
        );
        out.products = (top.rows || []).map(function (row) {
          var o = rowObj(top, row);
          return {
            name: o.PRODUCT_NAME,
            revenue: Number(o.REV) || 0,
            qty: Number(o.QTY) || 0,
          };
        });
      } catch (_topPg) {
        try {
          var top2 = await runSql(
            "SELECT " +
              quoteIdent("product_name", "postgres") +
              " AS product_name, SUM((" +
              quoteIdent("line_total", "postgres") +
              ")::numeric) AS rev, SUM((" +
              quoteIdent("quantity", "postgres") +
              ")::numeric) AS qty FROM " +
              q(tables.orderItems) +
              " GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 8"
          );
          out.products = (top2.rows || []).map(function (row) {
            var o = rowObj(top2, row);
            return {
              name: o.PRODUCT_NAME,
              revenue: Number(o.REV) || 0,
              qty: Number(o.QTY) || 0,
            };
          });
        } catch (_e10) {
          /* soft */
        }
      }
    }

    if (tables.stores) {
      try {
        var st = await runSql("SELECT COUNT(*) AS stores FROM " + q(tables.stores));
        out.c360.stores = Number(st.rows && st.rows[0] && st.rows[0][0]) || 0;
      } catch (_e11) {
        /* soft */
      }
    }

    return out;
  }

  function availableSliceModes(kpis) {
    return SLICE_DEFS.filter(function (d) {
      if (d.id === "channel") return (kpis.channel || []).length > 0;
      if (d.id === "state") return (kpis.state || []).length > 0;
      if (d.id === "tier") return (kpis.tier || []).length > 0;
      if (d.id === "payment") return (kpis.payment || []).length > 0;
      return false;
    });
  }

  function breakdownForSlice(kpis, slice) {
    if (slice === "state") return kpis.state || [];
    if (slice === "tier") return kpis.tier || [];
    if (slice === "payment") return kpis.payment || [];
    return kpis.channel || [];
  }

  function renderKpis(kpis) {
    var row = $("#rptKpiRow");
    if (!row) return;
    var h = kpis.headline || {};
    var cards = [
      { label: "Customers", value: fmtNum(h.customers), sub: "Customer 360 base", accent: false },
      { label: "Revenue", value: fmtMoney(h.revenue), sub: "Account / order sales", accent: true },
      {
        label: "Orders / txns",
        value: fmtNum(h.orders != null ? h.orders : h.txns),
        sub: h.orders != null ? "Distinct orders" : "Account transactions",
        accent: false,
      },
      {
        label: "Members",
        value: fmtNum(h.members),
        sub: (kpis.tier || []).length + " loyalty tiers",
        accent: false,
      },
    ];
    row.innerHTML = cards
      .map(function (c) {
        return (
          '<article class="rpt-kpi' +
          (c.accent ? " accent" : "") +
          '">' +
          '<p class="lbl">' +
          escapeHtml(c.label) +
          "</p>" +
          '<p class="val">' +
          escapeHtml(c.value) +
          "</p>" +
          '<p class="sub">' +
          escapeHtml(c.sub) +
          "</p>" +
          "</article>"
        );
      })
      .join("");
  }

  function renderSliceModes(modes) {
    var wrap = $("#rptSliceModes");
    if (!wrap) return;
    if (!modes.length) {
      wrap.innerHTML = '<span class="rpt-muted">No slice dimensions available in this schema.</span>';
      return;
    }
    if (!modes.some(function (m) { return m.id === state.scope.slice; })) {
      state.scope.slice = modes[0].id;
    }
    wrap.innerHTML = modes
      .map(function (m) {
        return (
          '<button type="button" class="rpt-mode' +
          (state.scope.slice === m.id ? " active" : "") +
          '" data-slice="' +
          escapeHtml(m.id) +
          '">' +
          escapeHtml(m.label) +
          "</button>"
        );
      })
      .join("");
  }

  function renderChips(breakdown) {
    var wrap = $("#rptChips");
    if (!wrap) return;
    var total = breakdown.reduce(function (s, b) {
      return s + b.value;
    }, 0);
    var chips = [{ key: "all", label: "All", value: total }].concat(
      breakdown.map(function (b) {
        return { key: b.key, label: prettyLabel(b.key), value: b.value };
      })
    );
    wrap.innerHTML = chips
      .map(function (c) {
        return (
          '<button type="button" class="rpt-chip' +
          (state.activeChip === c.key ? " active" : "") +
          '" data-chip="' +
          escapeHtml(c.key) +
          '">' +
          escapeHtml(c.label) +
          " · " +
          fmtNum(c.value) +
          "</button>"
        );
      })
      .join("");
  }

  function renderBarChart(breakdown, def) {
    var el = $("#rptBarChart");
    var hint = $("#rptBarHint");
    var title = $("#rptBarTitle");
    if (title && def) title.textContent = def.barTitle;
    if (hint && def) {
      hint.textContent =
        def.barHint +
        (state.activeChip !== "all" ? " · " + prettyLabel(state.activeChip) : "");
    }
    if (!el) return;
    var data =
      state.activeChip === "all"
        ? breakdown
        : breakdown.filter(function (b) {
            return b.key === state.activeChip;
          });
    if (!data.length) {
      el.innerHTML = '<div class="rpt-muted">No values for this slice.</div>';
      return;
    }
    var max = Math.max.apply(
      null,
      data.map(function (b) {
        return b.value;
      })
    );
    var w = 560;
    var h = 220;
    var pad = { l: 36, r: 16, t: 16, b: 48 };
    var innerW = w - pad.l - pad.r;
    var innerH = h - pad.t - pad.b;
    var barW = Math.min(48, innerW / data.length - 12);
    var svg =
      '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="Slice bar chart">';
    data.forEach(function (b, i) {
      var x = pad.l + (i + 0.5) * (innerW / data.length) - barW / 2;
      var bh = max ? (b.value / max) * innerH : 0;
      var y = pad.t + innerH - bh;
      var color = PALETTE[i % PALETTE.length];
      svg +=
        '<rect x="' +
        x +
        '" y="' +
        y +
        '" width="' +
        barW +
        '" height="' +
        Math.max(bh, 2) +
        '" rx="4" fill="' +
        color +
        '"></rect>';
      svg +=
        '<text x="' +
        (x + barW / 2) +
        '" y="' +
        (h - 18) +
        '" text-anchor="middle" font-size="10" fill="' +
        ACL.gray +
        '">' +
        escapeHtml(prettyLabel(b.key).slice(0, 12)) +
        "</text>";
      svg +=
        '<text x="' +
        (x + barW / 2) +
        '" y="' +
        (y - 6) +
        '" text-anchor="middle" font-size="11" font-weight="700" fill="' +
        ACL.navyDeep +
        '">' +
        fmtNum(b.value) +
        "</text>";
    });
    svg += "</svg>";
    var legend =
      '<div class="rpt-legend">' +
      data
        .map(function (b, i) {
          return (
            '<span><i style="background:' +
            PALETTE[i % PALETTE.length] +
            '"></i>' +
            escapeHtml(prettyLabel(b.key)) +
            "</span>"
          );
        })
        .join("") +
      "</div>";
    el.innerHTML = svg + legend;
  }

  function renderTrendChart(series) {
    var el = $("#rptTrendChart");
    var hint = $("#rptTrendHint");
    if (!el) return;
    if (hint) {
      hint.textContent =
        "Monthly revenue" +
        (state.activeChip !== "all" && state.scope.slice === "payment"
          ? " · illustrative under payment dice"
          : " from account transactions");
    }
    if (!series || !series.length) {
      el.innerHTML = '<div class="rpt-muted">No sales trend available in this schema (need accounts / sales table).</div>';
      return;
    }
    var values = series.map(function (s) {
      return s.value;
    });
    var w = 520;
    var h = 220;
    var pad = { l: 40, r: 12, t: 18, b: 36 };
    var innerW = w - pad.l - pad.r;
    var innerH = h - pad.t - pad.b;
    var max = Math.max.apply(null, values.concat([1]));
    var min = 0;
    var pts = series.map(function (s, i) {
      var x = pad.l + (series.length === 1 ? innerW / 2 : (i / (series.length - 1)) * innerW);
      var y = pad.t + innerH - ((s.value - min) / (max - min || 1)) * innerH;
      return [x, y, s];
    });
    var line = pts
      .map(function (p, i) {
        return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      })
      .join(" ");
    var area =
      line +
      " L " +
      pts[pts.length - 1][0].toFixed(1) +
      " " +
      (pad.t + innerH) +
      " L " +
      pts[0][0].toFixed(1) +
      " " +
      (pad.t + innerH) +
      " Z";
    var labels = "";
    series.forEach(function (s, i) {
      if (i % Math.ceil(series.length / 6) !== 0 && i !== series.length - 1) return;
      var x = pts[i][0];
      labels +=
        '<text x="' +
        x.toFixed(1) +
        '" y="' +
        (h - 12) +
        '" text-anchor="middle" font-size="10" fill="' +
        ACL.gray +
        '">' +
        escapeHtml(String(s.key).slice(2)) +
        "</text>";
    });
    el.innerHTML =
      '<svg viewBox="0 0 ' +
      w +
      " " +
      h +
      '" role="img" aria-label="Sales trend">' +
      '<defs><linearGradient id="rptGrad" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' +
      ACL.orange +
      '" stop-opacity="0.35"/>' +
      '<stop offset="100%" stop-color="' +
      ACL.navy +
      '" stop-opacity="0.02"/>' +
      "</linearGradient></defs>" +
      '<path d="' +
      area +
      '" fill="url(#rptGrad)"></path>' +
      '<path d="' +
      line +
      '" fill="none" stroke="' +
      ACL.navy +
      '" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>' +
      pts
        .map(function (p) {
          return (
            '<circle cx="' +
            p[0].toFixed(1) +
            '" cy="' +
            p[1].toFixed(1) +
            '" r="3.2" fill="' +
            ACL.orange +
            '" stroke="#fff" stroke-width="1.5"><title>' +
            escapeHtml(p[2].key + ": " + fmtMoney(p[2].value)) +
            "</title></circle>"
          );
        })
        .join("") +
      labels +
      "</svg>";
  }

  function renderC360(kpis) {
    var el = $("#rptC360");
    var hint = $("#rptC360Hint");
    if (!el) return;
    if (hint) {
      hint.textContent =
        "Profile coverage" +
        (state.activeChip !== "all" ? " · " + prettyLabel(state.activeChip) : "");
    }
    var c = kpis.c360 || {};
    var channelTop = (kpis.channel || []).slice(0, 4);
    var stateTop = (kpis.state || []).slice(0, 4);
    var tierTop = (kpis.tier || []).slice(0, 4);
    if (!channelTop.length && !stateTop.length && !tierTop.length && c.cities == null) {
      el.innerHTML = '<div class="rpt-muted">No customer profile tables found in this schema.</div>';
      return;
    }
    function block(title, rows) {
      return (
        '<div class="rpt-c360-block"><h3>' +
        escapeHtml(title) +
        "</h3>" +
        rows
          .map(function (r) {
            return (
              '<div class="rpt-c360-stat"><span class="k">' +
              escapeHtml(r.k) +
              '</span><span class="v">' +
              escapeHtml(r.v) +
              "</span></div>"
            );
          })
          .join("") +
        "</div>"
      );
    }
    el.innerHTML =
      block("Coverage", [
        { k: "Customers", v: fmtNum(kpis.headline && kpis.headline.customers) },
        { k: "Cities", v: fmtNum(c.cities) },
        { k: "States", v: fmtNum(c.states) },
        { k: "Stores", v: fmtNum(c.stores) },
      ]) +
      block(
        "Top channels",
        channelTop.length
          ? channelTop.map(function (x) {
              return { k: prettyLabel(x.key), v: fmtNum(x.value) };
            })
          : [{ k: "—", v: "n/a" }]
      ) +
      block(
        "Loyalty",
        tierTop.length
          ? tierTop.map(function (x) {
              return { k: prettyLabel(x.key), v: fmtNum(x.value) };
            })
          : [{ k: "Members", v: fmtNum(kpis.headline && kpis.headline.members) }]
      );
  }

  function renderProducts(products) {
    var el = $("#rptProducts");
    if (!el) return;
    if (!products || !products.length) {
      el.innerHTML = '<div class="rpt-muted">No order-item product sales in this schema.</div>';
      return;
    }
    el.innerHTML =
      '<table class="rpt-table" aria-label="Top products"><thead><tr><th>Product</th><th class="num">Qty</th><th class="num">Revenue</th></tr></thead><tbody>' +
      products
        .map(function (p) {
          return (
            "<tr><td class=\"nm\">" +
            escapeHtml(p.name || "—") +
            '</td><td class="num">' +
            escapeHtml(fmtNum(p.qty)) +
            '</td><td class="num">' +
            escapeHtml(fmtMoney(p.revenue)) +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody></table>";
  }

  function renderDetailTable(kpis) {
    var el = $("#rptTable");
    var title = $("#rptDetailTitle");
    var hint = $("#rptDetailHint");
    if (!el) return;
    if (state.scope.slice === "payment" && (kpis.payment || []).length) {
      if (title) title.textContent = "Payment methods";
      if (hint) hint.textContent = "Account transaction mix for the selected dice";
      var pays =
        state.activeChip === "all"
          ? kpis.payment
          : kpis.payment.filter(function (p) {
              return p.key === state.activeChip;
            });
      el.innerHTML =
        '<table class="rpt-table"><thead><tr><th>Method</th><th class="num">Transactions</th></tr></thead><tbody>' +
        pays
          .map(function (p) {
            return (
              "<tr><td class=\"nm\">" +
              escapeHtml(prettyLabel(p.key)) +
              '</td><td class="num">' +
              escapeHtml(fmtNum(p.value)) +
              "</td></tr>"
            );
          })
          .join("") +
        "</tbody></table>";
      return;
    }
    if (title) title.textContent = "Customer sample";
    if (hint) {
      hint.textContent =
        "Customer 360 records" +
        (state.activeChip !== "all" ? " · " + prettyLabel(state.activeChip) : "");
    }
    var rows = kpis.customers || [];
    if (!rows.length) {
      el.innerHTML = '<div class="rpt-muted">No customer rows for this dice selection.</div>';
      return;
    }
    el.innerHTML =
      '<table class="rpt-table" aria-label="Customer sample"><thead><tr><th>Customer</th><th>Name</th><th>City</th><th>State</th><th>Channel</th></tr></thead><tbody>' +
      rows
        .map(function (r) {
          return (
            "<tr><td class=\"nm\">" +
            escapeHtml(r.CUSTOMER_ID || "—") +
            "</td><td>" +
            escapeHtml(((r.FIRST_NAME || "") + " " + (r.LAST_NAME || "")).trim() || "—") +
            "</td><td>" +
            escapeHtml(r.CITY || "—") +
            "</td><td>" +
            escapeHtml(r.STATE || "—") +
            "</td><td>" +
            escapeHtml(prettyLabel(r.PREFERRED_CHANNEL)) +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody></table>";
  }

  function refreshVisuals() {
    var kpis = state.kpis;
    if (!kpis) return;
    var modes = availableSliceModes(kpis);
    renderSliceModes(modes);
    var def = SLICE_DEFS.find(function (d) {
      return d.id === state.scope.slice;
    }) || SLICE_DEFS[0];
    var breakdown = breakdownForSlice(kpis, state.scope.slice);
    renderChips(breakdown);
    renderKpis(kpis);
    renderBarChart(breakdown, def);
    renderTrendChart(kpis.salesTrend || []);
    renderC360(kpis);
    renderProducts(kpis.products || []);
    renderDetailTable(kpis);
  }

  async function applyDashboard() {
    var connectorId = ($("#rptConnection") && $("#rptConnection").value) || "";
    var database = ($("#rptDatabase") && $("#rptDatabase").value) || "";
    var schema = ($("#rptSchema") && $("#rptSchema").value) || "";
    if (!connectorId || !database || !schema) {
      setError("Select connection, database, and schema.");
      return;
    }
    setError("");
    var conn = state.connectors.find(function (c) {
      return c.id === connectorId;
    });
    var platform = String((conn && (conn.platform || conn.cloud)) || "").toLowerCase();
    state.scope = {
      connectorId: connectorId,
      database: database,
      schema: schema,
      slice: state.scope.slice || "channel",
      platform: platform,
    };
    state.activeChip = "all";
    setStatus("Loading business KPIs…");
    var applyBtn = $("#rptApplyBtn");
    if (applyBtn) applyBtn.disabled = true;
    try {
      if (typeof global.DataHiveAssets !== "undefined") {
        var catalog = await global.DataHiveAssets.catalog(connectorId);
        state.catalogItems = catalog.items || state.catalogItems;
      }
      var tables = resolveTables();
      if (!tables.customer && !tables.accounts && !tables.orderItems && !tables.memberships) {
        throw new Error(
          "No business tables found in " +
            schema +
            ". Expected customer / accounts / order_items / memberships (e.g. DH_POC_*)."
        );
      }
      state.kpis = await fetchBusinessKpis(tables);
      if (!state.kpis.available.length) {
        throw new Error("Could not query business KPIs for this scope. Check connector access.");
      }
      showDashboard();
      refreshVisuals();
      setStatus(
        "Customer 360 · sales · delivery channels — " +
          (conn && conn.display_name ? conn.display_name : connectorId) +
          " / " +
          database +
          " / " +
          schema
      );
      if (global.lucide && typeof global.lucide.createIcons === "function") {
        global.lucide.createIcons({ attrs: { "stroke-width": "1.75", "aria-hidden": "true" } });
      }
    } catch (err) {
      setError(err && err.message ? err.message : String(err));
      setStatus("");
      hideDashboard();
    } finally {
      if (applyBtn) applyBtn.disabled = false;
    }
  }

  async function onDiceChange() {
    if (!state.kpis) return;
    // Re-query customer-scoped KPIs when channel/state dice changes.
    if (state.scope.slice === "channel" || state.scope.slice === "state") {
      setStatus("Updating dice…");
      try {
        var tables = resolveTables();
        var next = await fetchBusinessKpis(tables);
        // Keep non-customer series stable when only customer filter applies.
        next.salesTrend = state.kpis.salesTrend;
        next.products = state.kpis.products;
        next.payment = state.kpis.payment;
        if (state.scope.slice !== "tier") next.tier = state.kpis.tier;
        if (state.activeChip === "all" || state.scope.slice !== "channel") {
          /* channel list already filtered */
        }
        state.kpis = next;
      } catch (_err) {
        /* keep prior */
      }
      setStatus("Dice updated.");
    }
    refreshVisuals();
  }

  function resetDashboard() {
    var conn = $("#rptConnection");
    if (conn) conn.value = "";
    resetDownstream("connection");
    hideDashboard();
    setError("");
    setStatus("");
    state.catalogItems = [];
    state.kpis = null;
    state.activeChip = "all";
    state.scope.slice = "channel";
  }

  function bindEvents() {
    var conn = $("#rptConnection");
    if (conn) conn.addEventListener("change", onConnectionChange);
    var db = $("#rptDatabase");
    if (db) db.addEventListener("change", onDatabaseChange);
    var sch = $("#rptSchema");
    if (sch) sch.addEventListener("change", onSchemaChange);
    var apply = $("#rptApplyBtn");
    if (apply) apply.addEventListener("click", applyDashboard);
    var reset = $("#rptResetBtn");
    if (reset) reset.addEventListener("click", resetDashboard);
    var modes = $("#rptSliceModes");
    if (modes) {
      modes.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-slice]");
        if (!btn) return;
        state.scope.slice = btn.getAttribute("data-slice") || "channel";
        state.activeChip = "all";
        if (state.kpis) refreshVisuals();
      });
    }
    var chips = $("#rptChips");
    if (chips) {
      chips.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-chip]");
        if (!btn) return;
        state.activeChip = btn.getAttribute("data-chip") || "all";
        onDiceChange();
      });
    }
  }

  async function init() {
    if (!state.initialized) {
      bindEvents();
      state.initialized = true;
    }
    await loadConnectors();
    if (global.lucide && typeof global.lucide.createIcons === "function") {
      global.lucide.createIcons({ attrs: { "stroke-width": "1.75", "aria-hidden": "true" } });
    }
    setStatus("Choose a connection to begin.");
  }

  global.DataHiveReporting = {
    init: init,
    apply: applyDashboard,
  };
})(window);
