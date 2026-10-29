// Pipelines list page: client-side filter, kind tiles, and live count.
//
// Progressive enhancement: the server renders every card and every group
// open, so with JavaScript disabled the page is fully usable. This module
// only hides/shows already-rendered cards (``.pipeline-row``), keeps the
// per-kind stat tiles in sync with the type select, and updates the
// "N of M shown" counter. No third-party dependencies, same-origin only.

const bar = document.querySelector("[data-pipeline-filters]");
if (bar) {
  const inputs = Array.from(bar.querySelectorAll("[data-filter]"));
  const rows = Array.from(document.querySelectorAll(".pipeline-row"));
  const groups = Array.from(document.querySelectorAll(".pipeline-group"));
  const tiles = Array.from(document.querySelectorAll("[data-kind-tile]"));
  const kindSelect = bar.querySelector('[data-filter="kind"]');
  const counter = bar.querySelector("[data-pl-count]");
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
    // Hide a whole group block when its cards are all filtered out.
    for (const group of groups) {
      const groupRows = group.querySelectorAll(".pipeline-row");
      const anyVisible = Array.from(groupRows).some((r) => !r.hidden);
      group.hidden = !anyVisible;
    }
    for (const tile of tiles) {
      tile.setAttribute("aria-pressed", c.kind === tile.dataset.kindTile ? "true" : "false");
    }
    if (counter) {
      counter.textContent =
        visibleTotal === rows.length ? `${rows.length} total` : `${visibleTotal} of ${rows.length} shown`;
    }
    if (noMatches) noMatches.hidden = visibleTotal !== 0;
  };

  for (const el of inputs) {
    const evt = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(evt, apply);
  }
  // Kind tiles toggle the type filter (click again to clear).
  for (const tile of tiles) {
    tile.addEventListener("click", () => {
      if (!kindSelect) return;
      const kind = tile.dataset.kindTile;
      kindSelect.value = kindSelect.value === kind ? "" : kind;
      apply();
    });
  }
  apply();
}
