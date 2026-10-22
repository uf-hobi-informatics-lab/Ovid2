/* Storage-account folder browser (pipeline-centric panel).
 *
 * Powers the graphical storage picker inside each Input / Output card on
 * the submit page. Each picker is a self-contained widget rooted at an
 * element carrying ``data-storage-picker="<role>"`` and declaring the
 * two hidden inputs the choice is written into:
 *
 *   data-connection-field="input_connection"   (the storage account name)
 *   data-location-field="input_location"       (the browsed folder path)
 *
 * The widget renders:
 *   - a storage-account <select> (populated server-side),
 *   - a breadcrumb trail,
 *   - a lazily-fetched listing with folder / file / database / table
 *     icons (folders + databases + schemas navigable, files + tables
 *     selectable),
 *   - a "Use this folder" affordance that writes the current prefix
 *     into the location field and records the storage account.
 *
 * It talks to GET /api/v1/storage/{name}/browse?prefix=… — the same
 * endpoint used for disc / s3 (filesystem prefixes) and Snowflake
 * (database → schema → table). ``supported=false`` renders a friendly
 * fallback. Pure vanilla ES5-ish so it runs without a build step.
 */
(function () {
  "use strict";

  // Inline Lucide (MIT) SVG markup — matches templates/_icons.html so
  // client-rendered rows look identical to server-rendered ones.
  var ICONS = {
    folder:
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/></svg>',
    file:
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    database:
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v6c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 11v6c0 1.66 4 3 9 3s9-1.34 9-3v-6"/></svg>',
    table:
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><path d="M12 3v18"/><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/></svg>',
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[<>&"']/g, function (c) {
      return {
        "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;",
      }[c];
    });
  }

  function StoragePicker(root) {
    this.root = root;
    this.connectionField = document.getElementsByName(
      root.getAttribute("data-connection-field") || ""
    )[0];
    this.locationField = document.getElementsByName(
      root.getAttribute("data-location-field") || ""
    )[0];
    this.select = root.querySelector("[data-role=storage-select]");
    this.crumbs = root.querySelector("[data-role=crumbs]");
    this.listing = root.querySelector("[data-role=listing]");
    this.chosen = root.querySelector("[data-role=chosen]");
    // Submit-page only: the owning Input/Output card carries the mode
    // toggle + hidden ``<role>_mode`` field. On the pipeline-config
    // builder these are absent, so every card-level operation below is
    // guarded and simply no-ops there.
    this.card = root.closest("[data-storage-card]");
    this.prefix = "";
    // Set from the browse response's ``type``. Warehouses
    // (snowflake/postgres/mssql/databricks) have no runnable root — a
    // database/schema must be chosen — so account-only selection is not a
    // complete choice for them. ``null`` until the first listing arrives.
    this.isWarehouse = null;
    this.bind();
    this.restore();
  }

  // Whether a storage ``type`` string names a warehouse (database.schema
  // .table) rather than a filesystem prefix. Kept in sync with
  // ``addresses_svc.WAREHOUSE_CONNECTION_TYPES`` on the server.
  StoragePicker.WAREHOUSE_TYPES = {
    snowflake: true,
    postgres: true,
    mssql: true,
    databricks: true,
  };

  // Reflect "an account is now chosen" onto the owning submit-page card:
  // flip its hidden ``<role>_mode`` to ``browse``, activate the matching
  // toggle button + pane, and clear any validation error. No-op when the
  // card / toggle isn't present (pipeline-config builder).
  StoragePicker.prototype.markBrowseActive = function () {
    var card = this.card;
    if (!card) return;
    var role = card.getAttribute("data-storage-card"); // input | output
    var modeField = card.querySelector('input[name="' + role + '_mode"]');
    if (modeField) modeField.value = "browse";
    var btns = card.querySelectorAll(".mode-btn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle(
        "active",
        btns[i].getAttribute("data-mode") === "browse"
      );
    }
    var panes = card.querySelectorAll(".mode-pane");
    for (var j = 0; j < panes.length; j++) {
      panes[j].hidden = panes[j].getAttribute("data-pane") !== "browse";
    }
    this.clearError();
  };

  StoragePicker.prototype.clearError = function () {
    if (this.card) this.card.classList.remove("invalid");
    var err = this.root.querySelector("[data-role=picker-error]");
    if (err) {
      err.hidden = true;
      err.textContent = "";
    }
  };

  StoragePicker.prototype.bind = function () {
    var self = this;
    if (this.select) {
      this.select.addEventListener("change", function () {
        self.prefix = "";
        self.isWarehouse = null;
        var account = self.select.value;
        if (self.connectionField) self.connectionField.value = account;
        if (self.locationField) self.locationField.value = "";
        if (account) {
          // Arm browse mode so the toggle + hidden mode field can't
          // disagree with the chosen account. Whether account-only is a
          // *complete* selection depends on the storage type, which we
          // only learn once the listing arrives: for a filesystem the
          // root is runnable; for a warehouse a schema must still be
          // chosen. ``render()`` finalises the chip + hidden fields once
          // ``body.type`` is known, so keep the chip neutral here.
          self.markBrowseActive();
          if (self.chosen) {
            self.chosen.innerHTML =
              ICONS.folder + " <strong>" + esc(account) + "</strong> &mdash; "
              + "browsing&hellip;";
            self.chosen.hidden = false;
          }
        } else if (self.chosen) {
          self.chosen.hidden = true;
        }
        self.refresh();
      });
    }
    if (this.listing) {
      this.listing.addEventListener("click", function (evt) {
        var t = evt.target.closest("[data-descend]");
        if (t) {
          evt.preventDefault();
          self.prefix = t.getAttribute("data-descend");
          self.refresh();
          return;
        }
        var use = evt.target.closest("[data-use]");
        if (use) {
          evt.preventDefault();
          self.choose(use.getAttribute("data-use"));
        }
      });
    }
    if (this.crumbs) {
      this.crumbs.addEventListener("click", function (evt) {
        var c = evt.target.closest("[data-crumb]");
        if (c) {
          evt.preventDefault();
          self.prefix = c.getAttribute("data-crumb");
          self.refresh();
        }
      });
    }
  };

  StoragePicker.prototype.restore = function () {
    // Re-seed the widget from a prior submission. On a full-page form
    // error re-render the server writes the operator's previous choice
    // back into the hidden fields and mirrors it on the picker root as
    // ``data-initial-connection`` / ``data-initial-location`` so the
    // chosen account + folder survive the failed submit instead of
    // silently resetting to blank.
    var conn =
      this.root.getAttribute("data-initial-connection") ||
      (this.connectionField && this.connectionField.value) ||
      "";
    var loc =
      this.root.getAttribute("data-initial-location") ||
      (this.locationField && this.locationField.value) ||
      "";
    if (!conn) return;
    if (this.select) this.select.value = conn;
    if (this.connectionField) this.connectionField.value = conn;
    if (this.locationField) this.locationField.value = loc;
    this.prefix = loc;
    // Show the "chosen" chip so the operator sees their prior pick, and
    // open the listing at that folder so they can keep browsing.
    if (this.chosen) {
      this.chosen.innerHTML =
        ICONS.folder + " <strong>" + esc(conn) + "</strong> / "
        + esc(loc || "(root)");
      this.chosen.hidden = false;
    }
    this.refresh();
  };

  StoragePicker.prototype.choose = function (path) {
    var account = this.select ? this.select.value : "";
    if (this.connectionField) this.connectionField.value = account;
    if (this.locationField) this.locationField.value = path || "";
    if (account) this.markBrowseActive();
    if (this.chosen) {
      this.chosen.innerHTML =
        ICONS.folder + " <strong>" + esc(account) + "</strong> / "
        + esc(path || "(root)");
      this.chosen.hidden = false;
    }
  };

  StoragePicker.prototype.renderCrumbs = function () {
    if (!this.crumbs) return;
    var parts = this.prefix ? this.prefix.split("/") : [];
    var html = '<a href="#" data-crumb="" class="crumb">root</a>';
    var acc = [];
    for (var i = 0; i < parts.length; i++) {
      acc.push(parts[i]);
      html += ' / <a href="#" data-crumb="' + esc(acc.join("/"))
        + '" class="crumb">' + esc(parts[i]) + "</a>";
    }
    this.crumbs.innerHTML = html;
  };

  StoragePicker.prototype.refresh = function () {
    var self = this;
    if (!this.select || !this.select.value) {
      if (this.listing) {
        this.listing.innerHTML =
          '<p class="note">Pick a storage account to browse.</p>';
      }
      return;
    }
    this.renderCrumbs();
    if (this.listing) {
      this.listing.innerHTML = '<p class="note">Loading&hellip;</p>';
    }
    var url = "/api/v1/storage/" + encodeURIComponent(this.select.value)
      + "/browse";
    if (this.prefix) url += "?prefix=" + encodeURIComponent(this.prefix);
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().then(function (b) {
          return { status: r.status, body: b };
        });
      })
      .then(function (res) {
        self.render(res);
      })
      .catch(function (err) {
        if (self.listing) {
          self.listing.innerHTML =
            '<p class="flash-error">Browse failed: ' + esc(err && err.message) + "</p>";
        }
      });
  };

  StoragePicker.prototype.render = function (res) {
    if (!this.listing) return;
    if (res.status >= 400) {
      this.listing.innerHTML =
        '<p class="flash-error">' + esc((res.body && res.body.detail) || ("HTTP " + res.status)) + "</p>";
      return;
    }
    var body = res.body || {};
    if (body.supported === false) {
      this.listing.innerHTML =
        '<p class="note">' + esc(body.message || "Browsing not supported for this storage type.")
        + " You can still pick a saved address instead.</p>";
      return;
    }
    var isWarehouse = (body.type === "snowflake" || body.type === "postgres"
      || body.type === "mssql" || body.type === "databricks");
    this.isWarehouse = isWarehouse;
    var atRoot = !this.prefix;
    // A warehouse at its root is NOT a runnable selection: a database
    // (and usually a schema) must be chosen. Keep the hidden location
    // empty and prompt the operator, mirroring the server-side guard in
    // ``addresses_svc.resolve_or_create_address``. Filesystems keep their
    // valid root, so their account-only chip stays a completed choice.
    if (this.chosen) {
      var account = this.select ? this.select.value : "";
      if (isWarehouse && atRoot) {
        this.chosen.innerHTML =
          ICONS.database + " <strong>" + esc(account) + "</strong> &mdash; "
          + '<span class="note">choose a database/schema below</span>';
      } else {
        this.chosen.innerHTML =
          (isWarehouse ? ICONS.database : ICONS.folder)
          + " <strong>" + esc(account) + "</strong> / "
          + esc(this.prefix || "(root)");
      }
      this.chosen.hidden = !account;
    }
    var dirIcon = isWarehouse ? ICONS.database : ICONS.folder;
    var leafIcon = isWarehouse ? ICONS.table : ICONS.file;

    var html = '<ul class="storage-list">';
    // Suppress the "use this <root>" affordance for a warehouse at root:
    // committing an empty location there is exactly the misconfiguration
    // this guard exists to prevent.
    if (!(isWarehouse && atRoot)) {
      html += '<li class="storage-use-here"><button type="button" class="button small" data-use="'
        + esc(this.prefix) + '">Use this ' + (isWarehouse ? "schema" : "folder")
        + ' (' + esc(this.prefix || "root") + ")</button></li>";
    } else {
      html += '<li class="note">Pick a database/schema below to run against '
        + "this warehouse.</li>";
    }
    var entries = body.entries || [];
    if (!entries.length) {
      html += '<li class="note">(empty)</li>';
    }
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      if (e.kind === "dir") {
        html += '<li class="entry entry-dir">' + dirIcon
          + ' <a href="#" data-descend="' + esc(e.path) + '">' + esc(e.name) + "</a>"
          + ' <button type="button" class="link-button small" data-use="'
          + esc(e.path) + '">use</button></li>';
      } else {
        html += '<li class="entry entry-leaf">' + leafIcon
          + ' <span class="mono">' + esc(e.name) + "</span>"
          + ' <button type="button" class="link-button small" data-use="'
          + esc(e.path) + '">use</button></li>';
      }
    }
    html += "</ul>";
    if (body.message) {
      html += '<p class="note">' + esc(body.message) + "</p>";
    }
    this.listing.innerHTML = html;
  };

  function init() {
    var roots = document.querySelectorAll("[data-storage-picker]");
    for (var i = 0; i < roots.length; i++) {
      new StoragePicker(roots[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
