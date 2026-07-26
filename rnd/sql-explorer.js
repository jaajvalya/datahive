/**
 * SQL workbench — schema tree, query editor, results (PostgreSQL via connector API).
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

  function headers(extra) {
    var h = { "X-DataHive-User": userHeader(), "Content-Type": "application/json" };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        h[k] = extra[k];
      });
    }
    return h;
  }

  async function fetchJson(path, options) {
    var res = await fetch(apiBase() + path, options || {});
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
      if (res.status === 404 && path.indexOf("/api/sql/") === 0) {
        detail =
          "SQL API not found (404). Stop the old server on port 5055, then run: cd rnd && python connector_api.py";
      } else if (res.status === 404) {
        detail =
          "Connector API route not found (404). Restart connector_api.py from the rnd folder.";
      }
      var err = new Error(detail);
      err.httpStatus = res.status;
      throw err;
    }
    return text ? JSON.parse(text) : {};
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function quoteIdent(part) {
    return '"' + String(part).replace(/"/g, '""') + '"';
  }

  function qualifiedTable(schema, table) {
    return quoteIdent(schema) + "." + quoteIdent(table);
  }

  var state = {
    initialized: false,
    lastResult: null,
    treeLoaded: false,
  };

  function $(sel) {
    return document.querySelector(sel);
  }

  function setStatus(msg, isErr) {
    var el = $("#sqlStatus");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("err", !!isErr);
  }

  function setResultsMessage(html) {
    var wrap = $("#sqlResultsWrap");
    if (wrap) wrap.innerHTML = html;
  }

  function renderResultsTable(result) {
    var wrap = $("#sqlResultsWrap");
    if (!wrap) return;
    if (!result || !result.columns || !result.columns.length) {
      wrap.innerHTML = '<div class="sql-results-empty">Query returned no columns.</div>';
      return;
    }
    var thead =
      "<thead><tr>" +
      result.columns
        .map(function (c) {
          return "<th>" + escapeHtml(c) + "</th>";
        })
        .join("") +
      "</tr></thead>";
    var bodyRows = (result.rows || []).map(function (row) {
      return (
        "<tr>" +
        row
          .map(function (cell) {
            if (cell === null || cell === undefined) {
              return '<td class="null">NULL</td>';
            }
            return "<td>" + escapeHtml(cell) + "</td>";
          })
          .join("") +
        "</tr>"
      );
    });
    var note = "";
    if (result.truncated) {
      note =
        '<p class="sql-results-note">Showing first ' +
        result.max_rows +
        " rows (result truncated).</p>";
    } else {
      note =
        '<p class="sql-results-note">' +
        result.row_count +
        " row" +
        (result.row_count === 1 ? "" : "s") +
        "</p>";
    }
    wrap.innerHTML =
      note +
      '<div class="sql-results-scroll"><table class="sql-results-table">' +
      thead +
      "<tbody>" +
      bodyRows.join("") +
      "</tbody></table></div>";
  }

  function rowsToCsv(columns, rows) {
    function esc(v) {
      if (v === null || v === undefined) return "";
      var s = String(v);
      if (/[",\n\r]/.test(s)) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    }
    var lines = [columns.map(esc).join(",")];
    (rows || []).forEach(function (row) {
      lines.push(row.map(esc).join(","));
    });
    return lines.join("\r\n");
  }

  function downloadCsv() {
    if (!state.lastResult || !state.lastResult.columns) {
      setStatus("Run a query with results before downloading.", true);
      return;
    }
    var csv = rowsToCsv(state.lastResult.columns, state.lastResult.rows || []);
    var blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "query-results-" + new Date().toISOString().slice(0, 19).replace(/:/g, "-") + ".csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setStatus("CSV downloaded.");
  }

  async function runQuery() {
    var ta = $("#sqlEditor");
    if (!ta) return;
    var sql = ta.value.trim();
    if (!sql) {
      setStatus("Enter a SQL query.", true);
      return;
    }
    var runBtn = $("#sqlRunBtn");
    if (runBtn) runBtn.disabled = true;
    setStatus("Running…");
    setResultsMessage('<div class="sql-results-empty">Executing query…</div>');
    try {
      var result = await fetchJson("/api/sql/query", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ sql: sql, max_rows: 1000 }),
      });
      state.lastResult = result;
      renderResultsTable(result);
      setStatus("Done.");
      var dl = $("#sqlDownloadBtn");
      if (dl) dl.disabled = !(result.rows && result.rows.length);
    } catch (err) {
      state.lastResult = null;
      var dlErr = $("#sqlDownloadBtn");
      if (dlErr) dlErr.disabled = true;
      var msg = err.message || String(err);
      if (msg === "Failed to fetch" || msg.indexOf("NetworkError") !== -1) {
        msg =
          "Cannot reach connector API at " +
          apiBase() +
          ". Start it with: cd rnd && python connector_api.py";
      }
      setResultsMessage(
        '<div class="sql-results-empty err">' + escapeHtml(msg) + "</div>"
      );
      setStatus(msg.length > 120 ? "Query failed — see details below." : msg, true);
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  }

  function appendToEditor(text) {
    var ta = $("#sqlEditor");
    if (!ta) return;
    var sep = ta.value.trim() ? "\n" : "";
    ta.value = ta.value + sep + text;
    ta.focus();
  }

  function treeNode(label, kind, extraClass, childrenHtml) {
    return (
      '<li class="sql-tree-node ' +
      (extraClass || "") +
      '" data-kind="' +
      escapeHtml(kind) +
      '">' +
      label +
      (childrenHtml ? '<ul class="sql-tree-children">' + childrenHtml + "</ul>" : "") +
      "</li>"
    );
  }

  function renderTreePlaceholder(msg) {
    var root = $("#sqlCatalogTree");
    if (root) root.innerHTML = '<li class="sql-tree-empty">' + escapeHtml(msg) + "</li>";
  }

  async function loadSchemasTree() {
    if (state.treeLoaded) return;
    renderTreePlaceholder("Loading schemas…");
    try {
      if (typeof global.DataHiveAssets === "undefined") {
        throw new Error("assets.js is not loaded.");
      }
      var data = await global.DataHiveAssets.schemas();
      var schemas = (data && data.items) || [];
      if (!schemas.length) {
        renderTreePlaceholder("No schemas found. Check POSTGRES_ASSET_SCHEMAS in .env.");
        return;
      }
      var root = $("#sqlCatalogTree");
      if (!root) return;
      root.innerHTML = schemas
        .map(function (schema) {
          return treeNode(
            '<button type="button" class="sql-tree-toggle" data-schema="' +
              escapeHtml(schema) +
              '" aria-expanded="false">' +
              '<span class="chev">▸</span> <span class="ic">📁</span> ' +
              escapeHtml(schema) +
              "</button>",
            "schema",
            "",
            ""
          );
        })
        .join("");
      state.treeLoaded = true;
      bindTreeEvents(root);
    } catch (err) {
      renderTreePlaceholder(err.message || String(err));
    }
  }

  function bindTreeEvents(root) {
    root.addEventListener("click", async function (e) {
      var schemaBtn = e.target.closest(".sql-tree-toggle[data-schema]");
      if (schemaBtn && !schemaBtn.dataset.table) {
        e.preventDefault();
        await toggleSchema(schemaBtn);
        return;
      }
      var tableBtn = e.target.closest(".sql-tree-toggle[data-table]");
      if (tableBtn) {
        e.preventDefault();
        await toggleTable(tableBtn);
        return;
      }
      var colBtn = e.target.closest(".sql-tree-col");
      if (colBtn) {
        e.preventDefault();
        appendToEditor(quoteIdent(colBtn.dataset.column || ""));
      }
    });

    root.addEventListener("dblclick", function (e) {
      var tableBtn = e.target.closest(".sql-tree-toggle[data-table]");
      if (!tableBtn) return;
      e.preventDefault();
      var schema = tableBtn.dataset.schema;
      var table = tableBtn.dataset.table;
      var ta = $("#sqlEditor");
      if (ta) {
        ta.value =
          "SELECT *\nFROM " +
          qualifiedTable(schema, table) +
          "\nLIMIT 100;";
      }
    });
  }

  async function toggleSchema(btn) {
    var schema = btn.dataset.schema;
    var expanded = btn.getAttribute("aria-expanded") === "true";
    var li = btn.closest(".sql-tree-node");
    var childUl = li && li.querySelector(":scope > .sql-tree-children");
    if (expanded) {
      btn.setAttribute("aria-expanded", "false");
      btn.querySelector(".chev").textContent = "▸";
      if (childUl) childUl.innerHTML = "";
      return;
    }
    btn.setAttribute("aria-expanded", "true");
    btn.querySelector(".chev").textContent = "▾";
    if (!childUl) {
      childUl = document.createElement("ul");
      childUl.className = "sql-tree-children";
      li.appendChild(childUl);
    }
    childUl.innerHTML = '<li class="sql-tree-empty">Loading tables…</li>';
    try {
      var data = await global.DataHiveAssets.tables(schema);
      var items = (data && data.items) || [];
      if (!items.length) {
        childUl.innerHTML = '<li class="sql-tree-empty">No tables in this schema.</li>';
        return;
      }
      childUl.innerHTML = items
        .map(function (t) {
          var name = t.name;
          var kind = t.type || "Table";
          return treeNode(
            '<button type="button" class="sql-tree-toggle" data-schema="' +
              escapeHtml(schema) +
              '" data-table="' +
              escapeHtml(name) +
              '" aria-expanded="false">' +
              '<span class="chev">▸</span> <span class="ic">' +
              (kind === "View" ? "👁" : "▦") +
              "</span> " +
              escapeHtml(name) +
              ' <span class="sql-tree-meta">' +
              escapeHtml(kind) +
              "</span></button>",
            "table",
            "",
            ""
          );
        })
        .join("");
    } catch (err) {
      childUl.innerHTML =
        '<li class="sql-tree-empty err">' + escapeHtml(err.message || String(err)) + "</li>";
    }
  }

  async function toggleTable(btn) {
    var schema = btn.dataset.schema;
    var table = btn.dataset.table;
    var expanded = btn.getAttribute("aria-expanded") === "true";
    var li = btn.closest(".sql-tree-node");
    var childUl = li && li.querySelector(":scope > .sql-tree-children");
    if (expanded) {
      btn.setAttribute("aria-expanded", "false");
      btn.querySelector(".chev").textContent = "▸";
      if (childUl) childUl.innerHTML = "";
      return;
    }
    btn.setAttribute("aria-expanded", "true");
    btn.querySelector(".chev").textContent = "▾";
    if (!childUl) {
      childUl = document.createElement("ul");
      childUl.className = "sql-tree-children";
      li.appendChild(childUl);
    }
    childUl.innerHTML = '<li class="sql-tree-empty">Loading columns…</li>';
    try {
      var data = await global.DataHiveAssets.structure(schema, table);
      var cols = (data && data.columns) || [];
      if (!cols.length) {
        childUl.innerHTML = '<li class="sql-tree-empty">No columns.</li>';
        return;
      }
      childUl.innerHTML = cols
        .map(function (c) {
          return (
            '<li class="sql-tree-node sql-tree-leaf">' +
            '<button type="button" class="sql-tree-col" data-column="' +
            escapeHtml(c.name) +
            '" title="Click to insert column name">' +
            '<span class="ic">▪</span> ' +
            escapeHtml(c.name) +
            ' <span class="sql-tree-meta">' +
            escapeHtml(c.type) +
            "</span></button></li>"
          );
        })
        .join("");
    } catch (err) {
      childUl.innerHTML =
        '<li class="sql-tree-empty err">' + escapeHtml(err.message || String(err)) + "</li>";
    }
  }

  function bindWorkbench() {
    var runBtn = $("#sqlRunBtn");
    if (runBtn) runBtn.addEventListener("click", runQuery);
    var dlBtn = $("#sqlDownloadBtn");
    if (dlBtn) {
      dlBtn.disabled = true;
      dlBtn.addEventListener("click", downloadCsv);
    }
    var ta = $("#sqlEditor");
    if (ta) {
      ta.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          runQuery();
        }
      });
    }
  }

  async function probeSqlApi() {
    try {
      var res = await fetch(apiBase() + "/api/sql/query", {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ sql: " " }),
      });
      if (res.status === 422) {
        setStatus("Ready — enter a query and click Run (or Ctrl+Enter).");
        return;
      }
      if (res.status === 404) {
        setStatus(
          "SQL API not found (404). Stop the old server on port 5055, then run: cd rnd && python connector_api.py",
          true
        );
        return;
      }
      if (!res.ok) {
        setStatus("Connector API error (HTTP " + res.status + ").", true);
      }
    } catch (_e) {
      setStatus("Connector API offline — run: cd rnd && python connector_api.py", true);
    }
  }

  function initSqlView() {
    if (!state.initialized) {
      bindWorkbench();
      state.initialized = true;
    }
    loadSchemasTree();
    probeSqlApi();
    setResultsMessage(
      '<div class="sql-results-empty">Run a query to see results here. Double-click a table in the tree for a starter SELECT.</div>'
    );
  }

  global.DataHiveSqlExplorer = {
    init: initSqlView,
    runQuery: runQuery,
  };
})(window);
