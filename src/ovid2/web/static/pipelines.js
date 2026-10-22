// Pipelines list page: client-side filter + per-type collapse.
//
// Progressive enhancement: the server renders every row and every group
// open, so with JavaScript disabled the page is fully usable. This module
// only hides/shows already-rendered rows and toggles the empty-state note.
// No third-party dependencies, same-origin only.

const bar = document.querySelector("[data-pipeline-filters]");
if (bar) {
  const inputs = Array.from(bar.querySelectorAll("[data-filter]"));
  const rows = Array.from(document.querySelectorAll(".pipeline-row"));
  const groups = Array.from(document.querySelectorAll(".pipeline-group"));
  const noMatches = document.getElementById("pf-no-matches");

  const criteria = () => {
    const c = {};
    for (const el of inputs) {
      c[el.dataset.filter] = el.value.trim().toLowerCase();
    }
    return c;
  };

  const rowMatches = (row, c) => {
    if (c.name && !row.dataset.name.includes(c.name)) return false;
    if (c.kind && row.dataset.kind !== c.kind) return false;
    if (c.trigger && row.dataset.trigger !== c.trigger) return false;
    if (c.enabled && row.dataset.enabled !== c.enabled) return false;
    if (c.cdm && !row.dataset.cdm.includes(c.cdm)) return false;
    return true;
  };

  const apply = () => {
    const c = criteria();
    let visibleTotal = 0;
    for (const row of rows) {
      const show = rowMatches(row, c);
      row.hidden = !show;
      if (show) visibleTotal += 1;
    }
    // Hide a whole group block when its rows are all filtered out.
    for (const group of groups) {
      const groupRows = group.querySelectorAll(".pipeline-row");
      const anyVisible = Array.from(groupRows).some((r) => !r.hidden);
      group.hidden = !anyVisible;
    }
    if (noMatches) noMatches.hidden = visibleTotal !== 0;
  };

  for (const el of inputs) {
    const evt = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(evt, apply);
  }
  apply();
}
