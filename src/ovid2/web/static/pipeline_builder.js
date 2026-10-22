/*
 * Ovid2 pipeline builder canvas.
 *
 * Drives the right-pane "ovid2" canvas on /pipelines/new: a live SVG
 * drawing of the pipeline as input -> body -> output, where the body
 * SHAPE reflects the pipeline type (kind) and the body MATERIAL/texture
 * reflects the engine. It also computes pipeline "completeness" and gates
 * the Save button + the canvas ``data-complete`` state on it.
 *
 * The module reads only the form's own inputs (by ``data-builder`` and
 * name); it does not fetch anything. The storage pickers (browse.js)
 * write their choices into hidden ``*_connection`` / ``*_location``
 * inputs, which we observe via a MutationObserver + input events.
 *
 * Dependency-free ES module, matching live.js / browse.js conventions.
 */

const SVGNS = "http://www.w3.org/2000/svg";

/*
 * Inline Lucide (MIT) glyph path-sets, kept byte-compatible with
 * templates/_icons.html so the canvas nodes look identical to the rest
 * of the UI. Each entry is an array of {tag, attrs} describing child
 * elements drawn in a 0..24 viewBox.
 */
const GLYPHS = {
  folder: [["path", { d: "M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z" }]],
  bucket: [
    ["path", { d: "M3 6h18l-1.5 13a2 2 0 0 1-2 1.8H6.5a2 2 0 0 1-2-1.8L3 6z" }],
    ["path", { d: "M2 6a10 3 0 1 0 20 0 10 3 0 1 0-20 0" }],
  ],
  database: [
    ["ellipse", { cx: "12", cy: "5", rx: "9", ry: "3" }],
    ["path", { d: "M3 5v6c0 1.66 4 3 9 3s9-1.34 9-3V5" }],
    ["path", { d: "M3 11v6c0 1.66 4 3 9 3s9-1.34 9-3v-6" }],
  ],
  file: [
    ["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" }],
    ["polyline", { points: "14 2 14 8 20 8" }],
  ],
};

/* Which glyph represents which connection TYPE. */
function iconForConnType(type) {
  const t = (type || "").toLowerCase();
  if (t === "s3" || t === "gcs" || t === "azure_blob") return "bucket";
  if (t === "snowflake" || t === "databricks" || t === "postgres" || t === "mssql") return "database";
  if (t === "disc" || t === "local") return "folder";
  if (!t || t === "unknown") return "folder";
  return "folder";
}

/*
 * Body silhouettes keyed by pipeline kind. Each returns a list of SVG
 * child specs drawn inside a 120x120 body group. The path uses the
 * ``builder-body-fill`` class so the engine gradient applies via
 * ``fill`` set on the group.
 */
function bodyShape(kind) {
  switch (kind) {
    case "etl":
      // Layered funnel — three stacked slabs narrowing downstream.
      return [
        ["polygon", { class: "builder-body-fill", points: "8,20 112,20 92,54 28,54" }],
        ["polygon", { class: "builder-body-fill", points: "28,58 92,58 78,86 42,86" }],
        ["polygon", { class: "builder-body-fill", points: "42,90 78,90 68,118 52,118" }],
      ];
    case "analysis":
      // Beaker / flask.
      return [
        ["path", { class: "builder-body-fill", d: "M48 14h24v6l-6 4v22l20 40a10 10 0 0 1-9 15H43a10 10 0 0 1-9-15l20-40V24l-6-4z" }],
      ];
    case "dq_checks":
      // Magnifier over a checkmark.
      return [
        ["circle", { class: "builder-body-fill", cx: "52", cy: "50", r: "34" }],
        ["polyline", { class: "builder-body-stroke", points: "38 50 48 62 68 36" }],
        ["line", { class: "builder-body-stroke", x1: "78", y1: "76", x2: "104", y2: "102", "stroke-width": "10" }],
      ];
    case "health":
      // Heartbeat pulse inside a rounded slab.
      return [
        ["rect", { class: "builder-body-fill", x: "10", y: "26", width: "100", height: "68", rx: "12" }],
        ["polyline", { class: "builder-body-stroke", points: "22 60 44 60 54 40 66 80 76 60 98 60" }],
      ];
    case "copy":
      // Two overlapping sheets.
      return [
        ["rect", { class: "builder-body-fill", x: "20", y: "20", width: "58", height: "58", rx: "8" }],
        ["rect", { class: "builder-body-fill", x: "44", y: "44", width: "58", height: "58", rx: "8" }],
      ];
    case "move":
      // Bold rightward arrow.
      return [
        ["polygon", { class: "builder-body-fill", points: "20,44 74,44 74,26 108,60 74,94 74,76 20,76" }],
      ];
    default:
      return [
        ["rect", { class: "builder-body-fill", x: "16", y: "24", width: "88", height: "72", rx: "10" }],
      ];
  }
}

/* Engine type -> material gradient id (defined in the template <defs>). */
function materialFor(engineType) {
  const t = (engineType || "").toLowerCase();
  if (t === "pyspark" || t === "databricks_spark" || t === "spark") return "mat-spark";
  if (t === "snowpark" || t === "snowflake") return "mat-snowpark";
  return "mat-unknown";
}

function el(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    node.setAttribute(k, String(v));
  }
  return node;
}

function drawGlyph(group, glyphName) {
  while (group.firstChild) group.removeChild(group.firstChild);
  const specs = GLYPHS[glyphName] || GLYPHS.folder;
  // Scale the 24-unit glyph up to fill the ~24px node icon slot.
  for (const [tag, attrs] of specs) {
    group.appendChild(el(tag, { ...attrs, class: "builder-glyph-path" }));
  }
}

function truncate(text, max) {
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

class PipelineBuilder {
  constructor(root) {
    this.root = root;
    this.form = document.getElementById("new-pipeline-form");
    this.canvas = document.getElementById("builder-canvas");
    this.saveBtn = document.getElementById("builder-save");
    this.playBtn = document.getElementById("builder-play");
    this.reqList = document.getElementById("builder-req-list");
    this.svg = this.canvas ? this.canvas.querySelector(".builder-svg") : null;
    this.wordmark = this.canvas ? this.canvas.querySelector(".builder-wordmark") : null;
    if (!this.form || !this.canvas || !this.svg) return;
    this.bind();
    this.syncStorageToggles();
    this.render();
  }

  field(name) {
    return this.form.querySelector('[data-builder="' + name + '"]');
  }

  /* The checked radio for a radio-group builder field, or null. */
  checkedRadio(name) {
    return this.form.querySelector(
      'input[type="radio"][data-builder="' + name + '"]:checked'
    );
  }

  value(name) {
    const node = this.field(name);
    if (!node) return "";
    // Radio groups: the first element's .value is not the selection.
    // Resolve the checked member instead.
    if (node.type === "radio") {
      const checked = this.checkedRadio(name);
      return checked ? String(checked.value || "").trim() : "";
    }
    return String(node.value || "").trim();
  }

  bind() {
    // Recompute on any change/input inside the form.
    const handler = () => this.render();
    this.form.addEventListener("change", handler);
    this.form.addEventListener("input", handler);
    // Show/hide each storage picker as its "bind now / keep open" radio
    // flips, then recompute.
    this.form.addEventListener("change", (evt) => {
      const t = evt.target;
      if (t && t.matches && t.matches('[data-storage-toggle]')) {
        this.syncStorageToggles();
        this.render();
      }
    });
    // The kind/trigger toggles fire this custom event after flipping
    // block visibility.
    document.addEventListener("ovid2:builder-refresh", handler);
    // The storage pickers write into hidden inputs programmatically
    // (browse.js sets .value without dispatching an event), so watch
    // them for attribute + value changes.
    for (const name of [
      "input_connection", "input_location",
      "output_connection", "output_location",
    ]) {
      const node = this.field(name);
      if (!node) continue;
      const obs = new MutationObserver(handler);
      obs.observe(node, { attributes: true, attributeFilter: ["value"] });
    }
    // browse.js sets .value directly, which does not mutate the attribute
    // or fire input; poll as a lightweight safety net so the canvas keeps
    // up with folder picks.
    this._lastSnapshot = "";
    this._poll = window.setInterval(() => {
      const snap = [
        this.value("input_connection"), this.value("input_location"),
        this.value("output_connection"), this.value("output_location"),
      ].join("|");
      if (snap !== this._lastSnapshot) {
        this._lastSnapshot = snap;
        this.render();
      }
    }, 400);
  }

  /* The chosen connection's declared type, read from the storage-select
   * option's data-type attribute. */
  connType(role) {
    const sel = this.form.querySelector(
      '[data-storage-picker="' + role + '"] [data-role=storage-select]'
    );
    if (!sel) return "";
    const opt = sel.options[sel.selectedIndex];
    return opt ? String(opt.getAttribute("data-type") || "") : "";
  }

  engineType() {
    // Engine is a radio group; read the checked radio's declared type.
    const checked = this.checkedRadio("engine");
    return checked ? String(checked.getAttribute("data-type") || "") : "";
  }

  /* True when the operator chose "keep open — choose at run time" for a
   * storage side (input / output). */
  isOpen(role) {
    return this.value(role + "_open") === "1";
  }

  /* True when the copy/move Tables card is set to "keep open" (tables
   * chosen at run time on the Submit form). */
  isTablesOpen() {
    return this.value("tables_open") === "1";
  }

  /* Show the storage picker only when its side is bound now; hide it when
   * the side is kept open (and clear any picked value so a stray
   * connection isn't submitted for an open side). */
  syncStorageToggles() {
    for (const role of ["input", "output"]) {
      const picker = this.form.querySelector(
        '[data-storage-picker="' + role + '"]'
      );
      if (!picker) continue;
      const open = this.isOpen(role);
      picker.style.display = open ? "none" : "";
      if (open) {
        const connField = this.field(role + "_connection");
        const locField = this.field(role + "_location");
        if (connField) connField.value = "";
        if (locField) locField.value = "";
      }
    }
    this.syncWatchTrigger();
  }

  /* A ``watch`` trigger enumerates new batch folders under the input
   * storage account, so it needs a BOUND input. When the input is kept
   * open, disable the watch option and fall back to a manual trigger. */
  syncWatchTrigger() {
    const trigger = document.getElementById("trigger_type");
    if (!trigger) return;
    const watchOpt = trigger.querySelector('option[value="watch"]');
    const inputOpen = this.isOpen("input");
    if (watchOpt) {
      watchOpt.disabled = inputOpen;
      watchOpt.title = inputOpen
        ? "Unavailable while the input is kept open — bind an input to watch it."
        : "";
    }
    if (inputOpen && trigger.value === "watch") {
      trigger.value = "manual";
      // Reflect the switch in the dependent trigger sub-fields.
      if (typeof window.ovid2_toggle_new_trigger_fields === "function") {
        window.ovid2_toggle_new_trigger_fields();
      }
    }
  }

  visible(id) {
    const node = document.getElementById(id);
    return node ? node.style.display !== "none" : false;
  }

  /* Compute the list of outstanding requirements. Empty => complete. */
  requirements() {
    const kind = this.value("kind") || "etl";
    const reqs = [];
    const name = this.value("name");
    if (!name) {
      reqs.push("give the pipeline a name");
    } else if (!/^[A-Za-z0-9._-]+$/.test(name)) {
      reqs.push("use only letters, numbers, . _ - in the name");
    }
    if (kind === "etl" && this.visible("template-block")) {
      const anyTmpl = this.form.querySelector('input[name="template"]:checked');
      if (!anyTmpl) reqs.push("pick an ETL template");
    }
    if (kind === "analysis" && !this.value("package")) {
      reqs.push("pick a pipeline package to analyse");
    }
    if (kind === "dq_checks") {
      const anyCycle = this.form.querySelector('input[name="packages"]:checked');
      if (!anyCycle) reqs.push("pick at least one EDC report cycle");
    }
    // copy/move transfer named tables verbatim (no ``all``). Require an
    // explicit list unless the operator kept the tables "open" (chosen
    // on the Submit form at run time).
    if ((kind === "copy" || kind === "move") && !this.isTablesOpen()) {
      if (!this.value("tables")) {
        reqs.push("list the tables to copy, or keep them open");
      }
    }
    if (!this.isOpen("input") && !this.value("input_connection")) {
      reqs.push("choose input storage (or keep it open)");
    }
    if (!this.isOpen("output") && !this.value("output_connection")) {
      reqs.push("choose output storage (or keep it open)");
    }
    if (!this.value("engine")) reqs.push("choose an engine");
    // A watch trigger cannot run against an open input.
    const trigger = document.getElementById("trigger_type");
    if (trigger && trigger.value === "watch" && this.isOpen("input")) {
      reqs.push("watch needs a bound input — bind the input or pick another trigger");
    }
    return reqs;
  }

  renderNode(role, connName, connType, location) {
    const g = this.svg.querySelector('[data-node="' + role + '"]');
    if (!g) return;
    const box = g.querySelector(".builder-node-box");
    const iconG = g.querySelector(".builder-node-icon");
    const label = g.querySelector(".builder-node-label");
    const open = this.isOpen(role);
    // "Open" reads as a satisfied-but-deferred node: solid box, a folder
    // glyph, and an "(open · at run time)" label — not the dashed
    // "(not set)" placeholder, which means the operator still owes a
    // choice.
    const set = Boolean(connName) || open;
    g.setAttribute("data-set", set ? "true" : "false");
    g.setAttribute("data-open", open ? "true" : "false");
    if (box) box.setAttribute("stroke-dasharray", set ? "0" : "6 5");
    drawGlyph(iconG, connName ? iconForConnType(connType) : "folder");
    if (label) {
      if (open) {
        label.textContent = "(open · run time)";
      } else if (connName) {
        const loc = location ? " / " + truncate(location, 14) : "";
        label.textContent = truncate(connName, 16) + loc;
      } else {
        label.textContent = "(not set)";
      }
    }
  }

  renderBody(kind, engineType) {
    const bodyG = this.svg.querySelector(".builder-body");
    const shapeG = bodyG ? bodyG.querySelector(".builder-body-shape") : null;
    const kindText = bodyG ? bodyG.querySelector(".builder-body-kind") : null;
    const cdmText = bodyG ? bodyG.querySelector(".builder-body-cdm") : null;
    if (!shapeG) return;
    while (shapeG.firstChild) shapeG.removeChild(shapeG.firstChild);
    for (const [tag, attrs] of bodyShape(kind)) {
      shapeG.appendChild(el(tag, attrs));
    }
    // Engine material: point the fill at the matching gradient.
    const mat = materialFor(engineType);
    shapeG.setAttribute("fill", "url(#" + mat + ")");
    bodyG.setAttribute("data-material", mat.replace("mat-", ""));
    if (kindText) kindText.textContent = kind;
    if (cdmText) {
      const cdm = this.value("cdm");
      cdmText.textContent = (this.visible("cdm-row") && cdm) ? cdm : "";
    }
  }

  render() {
    const kind = this.value("kind") || "etl";
    const engineType = this.engineType();

    this.renderNode(
      "input", this.value("input_connection"), this.connType("input"),
      this.value("input_location")
    );
    this.renderNode(
      "output", this.value("output_connection"), this.connType("output"),
      this.value("output_location")
    );
    this.renderBody(kind, engineType);

    const reqs = this.requirements();
    const complete = reqs.length === 0;
    this.canvas.setAttribute("data-complete", complete ? "true" : "false");
    if (this.saveBtn) this.saveBtn.disabled = !complete;
    if (this.wordmark) {
      this.wordmark.setAttribute("data-status", complete ? "running" : "queued");
    }
    if (this.playBtn) {
      // The pipeline is not saved yet, so play stays inert during
      // creation. It only titles differently once ready.
      this.playBtn.title = complete
        ? "Save the pipeline to run it"
        : "Complete the pipeline to enable saving";
    }
    if (this.reqList) {
      this.reqList.replaceChildren();
      if (complete) {
        const li = document.createElement("li");
        li.className = "builder-req-done small";
        li.textContent = "All set — you can create this pipeline.";
        this.reqList.appendChild(li);
      } else {
        for (const r of reqs) {
          const li = document.createElement("li");
          li.className = "builder-req small";
          li.textContent = r;
          this.reqList.appendChild(li);
        }
      }
    }
  }
}

function boot() {
  const root = document.getElementById("pipeline-builder");
  if (root) new PipelineBuilder(root);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
