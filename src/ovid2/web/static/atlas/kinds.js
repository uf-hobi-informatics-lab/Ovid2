/*
 * Ovid Atlas — node-kind renderer catalogue.
 *
 * One entry per server node kind (``atlas_svc.NODE_KINDS``; the two sets
 * are pinned equal by tests/contract/test_atlas_catalogue.py). Each entry
 * says which layer the kind lives on, its glyph, how wide its card is and
 * which dock tabs it offers. Adding a node kind is a new entry here *and*
 * in NODE_KINDS — never an ``if (kind === …)`` sprinkled in the canvas.
 */

export const LAYERS = ["algorithm", "compute", "storage"];

export const LAYER_LABEL = { algorithm: "algorithms", compute: "compute", storage: "storage" };

export const KINDS = {
  account:  { layer: "storage",   glyph: "◫", w: 210, tabs: ["overview", "data", "edit"], expandable: true },
  address:  { layer: "storage",   glyph: "▤", w: 190, tabs: ["overview", "data", "edit"], expandable: true },
  engine:   { layer: "compute",   glyph: "⚙", w: 190, tabs: ["overview", "edit"],         expandable: false },
  package:  { layer: "algorithm", glyph: "ƒ", w: 230, tabs: ["overview", "lanes", "edit"], expandable: true },
  config:   { layer: "algorithm", glyph: "▶", w: 230, tabs: ["overview", "run", "edit"],  expandable: true },
  workflow: { layer: "algorithm", glyph: "⛓", w: 220, tabs: ["overview", "edit"],         expandable: false },
};

/* Stage chain shown on an expanded config / table (mirrors atlas_svc.STAGES). */
export const STAGES = ["read", "format", "deduplicate", "transform", "map", "fix", "write"];

/* Stages that only build the lazy plan; ``write`` is where the engine works. */
export const LAZY_STAGES = new Set(["read", "format", "deduplicate", "transform", "map", "fix"]);

export const STAGE_SHORT = { read: "R", format: "F", deduplicate: "D", transform: "T", map: "M", fix: "X", write: "W" };

/* Edge kinds that are drawn as beams across planes. */
export const BEAM_EDGES = new Set(["reads", "writes", "runs_on", "implements"]);
