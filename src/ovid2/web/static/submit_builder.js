/*
 * Ovid2 job builder — page controller for /submit.
 *
 * Wires the left navigator (collapsible sections, kind chips, pipeline
 * cards, Source / Target storage trees, Options) to the right-hand job
 * graph, and gates the Submit button on completeness.
 *
 * State flows one way: user gestures → hidden form fields (the POST
 * contract is unchanged) → ``state`` → ``graph.render(state)``.
 *
 * Events consumed:
 *   ovid2:slot-change     {role, mode, label, type, …}  (storage_tree.js)
 *   ovid2:options-change                                (submit_plan.js)
 *   ovid2:graph-focus     {slot}                        (job_graph.js)
 * Events emitted:
 *   ovid2:kind-change     {kind, isCopyOrMove, isHealth}
 *   ovid2:pipeline-change {pipeline}
 *
 * Dependency-free ES module.
 */

import { hydrateLogos, logoForType } from "/static/logos.js";
import { StorageTree } from "/static/storage_tree.js";
import { JobGraph } from "/static/job_graph.js";

const COPY_KINDS = new Set(["copy", "move"]);
const HEALTH_KINDS = new Set(["health", "dq_checks"]);

class JobBuilder {
  constructor(form) {
    this.form = form;
    this.nav = document.getElementById("builder-nav");
    this.submitBtn = document.getElementById("builder-submit");
    this.pipelineField = document.getElementById("pipeline-field");
    this.freeName = form.querySelector("[data-role=free-name]");
    this.freeInput = document.getElementById("pipeline-free");
    this.batchField = document.getElementById("batch-field");
    this.slots = { input: null, output: null };
    this.kindDescriptions = this.readKindDescriptions();

    hydrateLogos(document);
    this.graph = new JobGraph(document.getElementById("job-graph"));
    this.trees = {};
    for (const root of form.querySelectorAll("[data-tree-role]")) {
      this.trees[root.getAttribute("data-tree-role")] = new StorageTree(root, form);
    }
    this.bind();
    this.applyKind(false);
    this.restorePipeline();
    // Open the first section that still needs input.
    this.openNextIncomplete(null);
    this.render();
  }

  readKindDescriptions() {
    const node = document.getElementById("kind-descriptions");
    const out = {};
    try {
      for (const k of JSON.parse(node ? node.textContent : "[]")) out[k.name] = k;
    } catch (_e) { /* keep empty */ }
    return out;
  }

  // ------------------------------------------------------------ getters

  kind() {
    const r = this.form.querySelector('input[name="kind"]:checked');
    return (r && r.value) || "etl";
  }

  section(name) {
    return this.form.querySelector('[data-builder-section="' + name + '"]');
  }

  // ------------------------------------------------------------- events

  bind() {
    // Kind chips.
    for (const radio of this.form.querySelectorAll('input[name="kind"]')) {
      radio.addEventListener("change", () => this.applyKind(true));
    }
    // Pipeline cards: clicking the icon (or anywhere on the card) selects.
    const cards = this.form.querySelector(".pipeline-cards");
    if (cards) {
      cards.addEventListener("click", (evt) => {
        const card = evt.target.closest(".pipeline-card");
        if (card && !card.hidden) this.choosePipeline(card.dataset.pipeline, true);
      });
    }
    if (this.freeInput) {
      this.freeInput.addEventListener("input", () => {
        this.pipelineField.value = this.freeInput.value.trim();
        this.markPipelineCards();
        this.render();
      });
      // Enter / leaving the field commits the name and moves on to the
      // next unassigned section (Enter must not submit the form here).
      this.freeInput.addEventListener("keydown", (evt) => {
        if (evt.key !== "Enter") return;
        evt.preventDefault();
        if (this.freeInput.value.trim()) this.openNextIncomplete("pipeline");
      });
      this.freeInput.addEventListener("change", () => {
        if (this.freeInput.value.trim()) this.openNextIncomplete("pipeline");
      });
    }
    // Accordion: opening one section closes the others.
    for (const sec of this.form.querySelectorAll("[data-builder-section]")) {
      sec.addEventListener("toggle", () => {
        if (!sec.open) return;
        for (const other of this.form.querySelectorAll("[data-builder-section]")) {
          if (other !== sec && other.open) other.open = false;
        }
      });
    }
    document.addEventListener("ovid2:slot-change", (evt) => {
      const d = evt.detail || {};
      if (d.role !== "input" && d.role !== "output") return;
      this.slots[d.role] = d.mode ? d : null;
      // The batch id is the complete Data Space path of the chosen input
      // (account + folder / DATABASE/SCHEMA). The server re-derives it;
      // this only shows it. A legacy saved address has none — the field
      // then keeps a server-preselected value, or the address name.
      if (d.role === "input" && this.batchField) {
        this.batchField.value = d.mode ? (d.batch || d.address || "") : "";
      }
      this.render();
      if (d.mode) this.openNextIncomplete(d.role);
    });
    document.addEventListener("ovid2:options-change", () => this.render());
    document.addEventListener("ovid2:graph-focus", (evt) => {
      const slot = (evt.detail || {}).slot;
      this.openSlot(slot);
    });
    for (const r of this.form.querySelectorAll('input[name="engine"]')) {
      r.addEventListener("change", () => this.render());
    }

    this.form.addEventListener("submit", (evt) => {
      const st = this.computeState();
      if (!st.complete) {
        evt.preventDefault();
        evt.stopImmediatePropagation();
        this.openSlot(st.firstMissingSlot);
      }
    }, true);
  }

  // --------------------------------------------------------------- kind

  applyKind(userInitiated) {
    const kind = this.kind();
    const isCopy = COPY_KINDS.has(kind);
    const isHealth = HEALTH_KINDS.has(kind);
    const show = (id, on) => {
      const el = document.getElementById(id);
      if (el) el.hidden = !on;
    };
    show("transformations-card", !(isCopy || isHealth));
    show("tables-card", !isHealth);
    show("health-card", isHealth);
    show("checks-card", isHealth);
    for (const id of ["sf-account-row", "stderr-log-row"]) {
      show(id, !isCopy);
    }
    show("extra-mounts-row", isCopy);
    show("strict-types-row", kind === "move");
    show("copy-hint", kind === "copy");
    show("move-hint", kind === "move");

    // Pipeline cards relevant to the kind: a package card is listed only
    // when a saved pipeline of this kind references it (``data-kinds``,
    // server-rendered from the pipeline configs); health kinds also get
    // the generic EDC labels; copy/move additionally take a free job name.
    let visible = 0;
    for (const card of this.form.querySelectorAll(".pipeline-card")) {
      const generic = card.dataset.group === "generic";
      const kinds = (card.dataset.kinds || "").split(/\s+/).filter(Boolean);
      card.hidden = generic ? !isHealth : !kinds.includes(kind);
      if (!card.hidden) visible++;
    }
    if (this.freeName) this.freeName.hidden = !isCopy;
    this.form.dataset.kind = kind;
    const empty = this.form.querySelector("[data-role=no-pipelines]");
    if (empty) {
      empty.hidden = visible > 0 || isCopy;
      const lbl = empty.querySelector("[data-role=no-pipelines-kind]");
      if (lbl) lbl.textContent = (this.kindDescriptions[kind] || {}).label || kind;
    }
    // A pipeline chosen for one job type never carries over to another:
    // a user-initiated switch always clears it (the initial render keeps
    // a ?pipeline= preselect); otherwise drop it only if it got hidden.
    const curName = this.pipelineField.value;
    const cur = curName
      ? this.form.querySelector('.pipeline-card[data-pipeline="' + CSS.escape(curName) + '"]')
      : null;
    if (userInitiated && curName) {
      if (this.freeInput) this.freeInput.value = "";
      this.choosePipeline("", false);
    } else if (cur && cur.hidden) {
      this.choosePipeline("", false);
    }

    const desc = this.form.querySelector("[data-role=kind-desc]");
    if (desc) desc.textContent = (this.kindDescriptions[kind] || {}).description || "";

    document.dispatchEvent(new CustomEvent("ovid2:kind-change", {
      detail: { kind, isCopyOrMove: isCopy, isHealth },
    }));
    if (userInitiated) this.render();
  }

  // ----------------------------------------------------------- pipeline

  choosePipeline(name, advance) {
    this.pipelineField.value = name || "";
    if (this.freeInput && name) this.freeInput.value = "";
    this.markPipelineCards();
    document.dispatchEvent(new CustomEvent("ovid2:pipeline-change", { detail: { pipeline: name } }));
    this.render();
    if (advance && name) this.openNextIncomplete("pipeline");
  }

  markPipelineCards() {
    const v = this.pipelineField.value;
    for (const card of this.form.querySelectorAll(".pipeline-card")) {
      const on = !!v && card.dataset.pipeline === v;
      card.classList.toggle("is-selected", on);
      card.setAttribute("aria-selected", on ? "true" : "false");
    }
  }

  restorePipeline() {
    const v = this.pipelineField.value;
    if (!v) return;
    const card = this.form.querySelector('.pipeline-card[data-pipeline="' + CSS.escape(v) + '"]');
    if (card && !card.hidden) {
      this.choosePipeline(v, false);
    } else if (this.freeInput && COPY_KINDS.has(this.kind())) {
      this.freeInput.value = v;
      this.markPipelineCards();
    }
  }

  // --------------------------------------------------------- navigation

  openSlot(slot) {
    // The batch id follows from the source — "batch" opens the Source.
    const map = { input: "input", output: "output", pipeline: "pipeline", batch: "input" };
    let secName = map[slot];
    let sub = null;
    if (!secName) {
      secName = "options";
      sub = slot; // tables | stages | checks | cycle | engine
    }
    const sec = this.section(secName);
    if (!sec) return;
    sec.open = true;
    if (sub) {
      const subEl = this.form.querySelector('[data-builder-sub="' + sub + '"]');
      if (subEl && !subEl.hidden) subEl.open = true;
    }
    sec.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  openNextIncomplete(justDone) {
    const st = this.computeState();
    const order = ["pipeline", "input", "output"];
    const next = order.find((s) => !st.done[s]);
    if (justDone) {
      const cur = this.section(justDone);
      if (cur && next) cur.open = false;
    }
    if (next) {
      const sec = this.section(next);
      if (sec) sec.open = true;
    }
  }

  // -------------------------------------------------------------- state

  computeState() {
    const kind = this.kind();
    const isCopy = COPY_KINDS.has(kind);
    const isHealth = HEALTH_KINDS.has(kind);
    const pipeline = (this.pipelineField.value || "").trim();
    const batch = (this.batchField ? this.batchField.value : "").trim();
    const src = this.slots.input;
    const dst = this.slots.output;

    const tBoxes = document.querySelectorAll(".table-check");
    const tChecked = document.querySelectorAll(".table-check:checked");
    const sBoxes = document.querySelectorAll(".stage-check");
    const sChecked = document.querySelectorAll(".stage-check:checked");
    const cBoxes = document.querySelectorAll(".check-check");
    const cChecked = document.querySelectorAll(".check-check:checked");

    const tablesOk = isHealth || !tBoxes.length || tChecked.length > 0;
    const stagesOk = isCopy || isHealth || !sBoxes.length || sChecked.length > 0;

    const done = {
      pipeline: !!pipeline,
      input: !!src,
      output: !!dst,
      batch: !!batch,
      tables: tablesOk,
      stages: stagesOk,
    };
    const labels = {
      pipeline: "pipeline", input: "source", output: "target", batch: "batch id (choose a source)",
      tables: "at least one table", stages: "at least one stage",
    };
    const missing = Object.keys(done).filter((k) => !done[k]);
    const complete = missing.length === 0;

    const engine = this.form.querySelector('input[name="engine"]:checked');
    const kindLabel = (this.kindDescriptions[kind] || {}).label || kind;

    const nodes = [
      {
        slot: "input", title: "Source", done: done.input,
        value: src ? src.label : "", sub: src ? (src.type || src.mode) : "",
        logo: src ? (src.mode === "address" && !src.type ? "saved" : logoForType(src.type)) : "folder",
      },
      {
        slot: "pipeline", title: kindLabel, done: done.pipeline,
        value: pipeline, sub: pipeline ? this.pipelineSub(pipeline) : "",
        logo: kind,
      },
      {
        slot: "output", title: "Target", done: done.output,
        value: dst ? dst.label : "", sub: dst ? (dst.type || dst.mode) : "",
        logo: dst ? (dst.mode === "address" && !dst.type ? "saved" : logoForType(dst.type)) : "folder",
      },
    ];

    const countLabel = (all, checked, none) => {
      if (!all.length) return none;
      if (checked.length === all.length) return "all (" + all.length + ")";
      return checked.length + " / " + all.length;
    };
    const chips = [
      {
        slot: "tables", label: "Tables", logo: "table", hidden: isHealth,
        value: countLabel(tBoxes, tChecked, isCopy ? "from source" : "all"),
        custom: tBoxes.length > 0 && tChecked.length !== tBoxes.length,
        done: tablesOk,
      },
      {
        slot: "stages", label: "Stages", logo: "etl", hidden: isCopy || isHealth,
        value: countLabel(sBoxes, sChecked, "all"),
        custom: sBoxes.length > 0 && sChecked.length !== sBoxes.length,
        done: stagesOk,
      },
      {
        slot: "checks", label: "Checks", logo: "dq_checks", hidden: !isHealth,
        value: countLabel(cBoxes, cChecked, "whole cycle"),
        custom: cBoxes.length > 0 && cChecked.length !== cBoxes.length && cChecked.length > 0,
      },
      {
        slot: "engine", label: "Engine", logo: "engine",
        value: engine ? engine.value : "pyspark",
        custom: !!engine && engine.value !== "pyspark",
      },
    ];

    const order = ["pipeline", "input", "output", "tables", "stages", "batch"];
    const firstMissingSlot = order.find((k) => !done[k]) || null;
    return {
      kind, done, nodes, chips, complete, firstMissingSlot,
      missing: missing.map((k) => labels[k]),
    };
  }

  pipelineSub(name) {
    const card = this.form.querySelector('.pipeline-card[data-pipeline="' + CSS.escape(name) + '"]');
    return card ? (card.dataset.sub || "") : "job name";
  }

  render() {
    const st = this.computeState();
    this.graph.render(st);
    if (this.submitBtn) {
      this.submitBtn.disabled = !st.complete;
      this.submitBtn.title = st.complete ? "" : "Still needed: " + st.missing.join(", ");
    }
    const set = (name, text, done) => {
      const el = this.form.querySelector('[data-summary="' + name + '"]');
      if (!el) return;
      el.textContent = text;
      el.classList.toggle("is-done", !!done);
      const sec = el.closest("[data-builder-section]");
      if (sec) sec.classList.toggle("is-done", !!done);
    };
    const kindLabel = (this.kindDescriptions[st.kind] || {}).label || st.kind;
    set("pipeline", st.done.pipeline ? kindLabel + " \u00b7 " + this.pipelineField.value : kindLabel + " \u00b7 choose a pipeline", st.done.pipeline);
    set("input", this.slots.input ? this.slots.input.label : "not chosen", st.done.input);
    set("output", this.slots.output ? this.slots.output.label : "not chosen", st.done.output);
    const custom = st.chips.filter((c) => !c.hidden && c.custom).map((c) => c.label.toLowerCase());
    const optEl = this.form.querySelector('[data-summary="options"]');
    if (optEl) optEl.textContent = custom.length ? "customised: " + custom.join(", ") : "defaults \u2014 optional";
  }
}

function boot() {
  const form = document.getElementById("submit-form");
  if (form && form.classList.contains("job-builder")) {
    window.ovid2JobBuilder = new JobBuilder(form);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
