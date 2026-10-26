/*
 * Ovid2 logo marks — simple, brand-*like* inline SVG glyphs.
 *
 * Every mark is drawn in a 24x24 viewBox with ``currentColor`` so CSS
 * decides the colour (see ``.logo-<name>`` rules in ovid2.css, which
 * tint storage types with a recognisable hue). These are original,
 * deliberately generic drawings — NOT vendor trademark files — so the
 * public mirror can ship them freely.
 *
 * Usage:
 *   import { logoSvg, logoForType, hydrateLogos } from "/static/logos.js";
 *   el.innerHTML = logoSvg("snowflake", 20);
 *   hydrateLogos(document);   // fills every <span data-logo="…">
 *
 * Dependency-free ES module.
 */

const STROKE =
  'fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"';

/* name -> inner SVG markup (24-unit viewBox). */
export const LOGOS = {
  // ---- storage types ------------------------------------------------
  snowflake:
    '<path d="M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7"/>' +
    '<path d="M9.5 3.5L12 6l2.5-2.5M9.5 20.5L12 18l2.5 2.5"/>' +
    '<path d="M3 10.5l3.3-.9-.9-3.3M21 13.5l-3.3.9.9 3.3"/>' +
    '<path d="M3 13.5l3.3.9-.9 3.3M21 10.5l-3.3-.9.9-3.3"/>',
  s3:
    '<path d="M4 6l1.8 13.2A2 2 0 0 0 7.8 21h8.4a2 2 0 0 0 2-1.8L20 6"/>' +
    '<ellipse cx="12" cy="6" rx="8" ry="3"/>' +
    '<path d="M7.5 12.5c3 1.2 6 1.2 9 0"/>',
  azure_blob:
    '<path d="M10.5 3L4 19.5h5.2L14 9.8z"/>' +
    '<path d="M13.2 12.6l2.6 4.2-6.6 2.7H20L15.2 5.5z"/>',
  databricks:
    '<path d="M12 3l9 5-9 5-9-5z"/>' +
    '<path d="M3 12.5l9 5 9-5"/>' +
    '<path d="M3 17l9 5 9-5"/>',
  gcs:
    '<path d="M7.5 3.5h9L21 12l-4.5 8.5h-9L3 12z"/>' +
    '<path d="M9 12h6M12 9v6"/>',
  postgres:
    '<ellipse cx="12" cy="5" rx="8" ry="3"/>' +
    '<path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>' +
    '<path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>' +
    '<path d="M15 9.5v5.5"/>',
  mssql:
    '<ellipse cx="12" cy="5" rx="8" ry="3"/>' +
    '<path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>' +
    '<path d="M7 14c2-2 3.5 1 5-1s3 1 5-1"/>',
  disc:
    '<rect x="2.5" y="6" width="19" height="12" rx="2"/>' +
    '<path d="M6 14h7"/><circle cx="17.5" cy="12" r="1.2"/>',
  access:
    '<rect x="3" y="3" width="18" height="18" rx="3"/>' +
    '<path d="M8 17l4-10 4 10M9.5 13.5h5"/>',
  // ---- tree nodes ---------------------------------------------------
  folder:
    '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/>',
  schema:
    '<ellipse cx="12" cy="5" rx="9" ry="3"/>' +
    '<path d="M3 5v6c0 1.66 4 3 9 3s9-1.34 9-3V5"/>' +
    '<path d="M3 11v6c0 1.66 4 3 9 3s9-1.34 9-3v-6"/>',
  table:
    '<rect width="18" height="18" x="3" y="3" rx="2"/>' +
    '<path d="M3 9h18M3 15h18M12 9v12"/>',
  file:
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
    '<polyline points="14 2 14 8 20 8"/>',
  saved:
    '<path d="M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
  // ---- pipeline kinds -----------------------------------------------
  etl:
    '<path d="M3 4h18l-7 8v6l-4 2v-8z"/>',
  analysis:
    '<path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3"/>' +
    '<path d="M7 15h10"/>',
  dq_checks:
    '<circle cx="10.5" cy="10.5" r="7"/><path d="M21 21l-5.5-5.5"/>' +
    '<path d="M7.5 10.5l2 2 3.5-4"/>',
  health:
    '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  copy:
    '<rect x="8" y="8" width="13" height="13" rx="2"/>' +
    '<path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2"/>',
  move:
    '<path d="M4 12h15M13 6l6 6-6 6"/>',
  // ---- misc ---------------------------------------------------------
  pipeline:
    '<circle cx="5" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/>' +
    '<rect x="9" y="8.5" width="6" height="7" rx="1.5"/>' +
    '<path d="M7.5 12H9M15 12h1.5"/>',
  engine:
    '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/>' +
    '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
  check:
    '<polyline points="20 6 9 17 4 12"/>',
  chevron:
    '<polyline points="9 18 15 12 9 6"/>',
  unknown:
    '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17h.01"/>',
};

/* Storage-connection TYPE -> logo name. */
export function logoForType(type) {
  const t = String(type || "").toLowerCase();
  return Object.prototype.hasOwnProperty.call(LOGOS, t) ? t : "unknown";
}

/* Legacy address ``src_name`` (disc / snowflake / …) -> logo name. */
export function logoForSrcName(src) {
  return logoForType(src);
}

/*
 * Return a complete ``<svg>`` string for ``name`` at ``size`` px. The
 * wrapper carries ``logo logo-<name>`` classes so CSS can tint it.
 */
export function logoSvg(name, size = 18, extraClass = "") {
  const key = Object.prototype.hasOwnProperty.call(LOGOS, name) ? name : "unknown";
  const cls = ("logo logo-" + key + " " + (extraClass || "")).trim();
  return (
    '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" ' +
    STROKE + ' class="' + cls + '" aria-hidden="true" focusable="false">' +
    LOGOS[key] + "</svg>"
  );
}

/* Fill every ``[data-logo]`` placeholder under ``root``. */
export function hydrateLogos(root) {
  const scope = root || document;
  for (const node of scope.querySelectorAll("[data-logo]")) {
    if (node.dataset.logoDone === "1") continue;
    const size = Number(node.getAttribute("data-logo-size") || 18);
    node.innerHTML = logoSvg(node.getAttribute("data-logo"), size);
    node.dataset.logoDone = "1";
  }
}
