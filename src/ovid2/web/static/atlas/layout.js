/*
 * Ovid Atlas — lane model: one line per workflow and per pipeline, grouped
 * and sized to fit the canvas exactly.
 *
 * The Atlas is a list of *lanes* stacked top → bottom in four groups, each
 * under its own header with a wide gap before it:
 *
 *   workflows   one lane per workflow: its steps are pipeline components
 *               laid out as the workflow's DAG (``dagLayout``: column =
 *               longest path from a source, row = barycentre of the
 *               upstream rows). ``data`` / ``after`` links and their
 *               ``on`` branch are drawn between the components;
 *   pipelines   one lane per run config no workflow uses — one pipeline
 *               component: name · shape (run kind · package) · material
 *               (engine type) · input → output;
 *   accounts    the storage accounts (Data Space entry points);
 *   unused      packages / engines no run config uses (unbound locations
 *               are listed under their account in the dock).
 *
 * ``fitLanes`` divides the canvas height between the lanes by weight (a
 * workflow lane weighs as many rows as its DAG has): rows shrink to a
 * compact one-line height, and only below that does the lane list scroll
 * (inside the canvas — the page never grows). With "running on top"
 * (``orderLanes``) a lane with a live job floats to the top of its group,
 * keeping the alphabetical order inside each group.
 *
 * Everything here is pure (no DOM) so tests/unit/web/test_atlas.py runs it
 * under node / bun.
 */

export const LANE_MIN = 32; // one-line cards
export const LANE_GAP = 10; // between two lanes
export const GROUP_GAP = 22; // extra space before a group header
export const GROUP_HEAD = 18; // a group header's own height
export const COMPACT_BELOW = 54; // under this a card is a single line
export const ROOMY_FROM = 96; // from this a config card shows its live chain

/* Groups top → bottom (a group with no lane is not drawn). */
export const GROUPS = [
  { key: "workflows", label: "Workflows" },
  { key: "pipelines", label: "Pipelines" },
  { key: "accounts", label: "Storage accounts" },
  { key: "unused", label: "Not used" },
];
const GROUP_OF = { workflow: "workflows", pipeline: "pipelines", accounts: "accounts", unused: "unused" };

/* Beam roles of a pipeline component, with the layer each needs. */
export const COLUMNS = [
  { key: "input", layer: "storage", label: "input" },
  { key: "engine", layer: "compute", label: "engine" },
  { key: "package", layer: "algorithm", label: "package" },
  { key: "output", layer: "storage", label: "output" },
];

const byName = (a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
const emptyBeam = () => ({ input: [], engine: [], package: [], output: [] });

/* config id → {input: [ids], engine: [ids], package: [ids], output: [ids]}. */
export function beams(graph) {
  const out = new Map();
  const get = (c) => {
    if (!out.has(c)) out.set(c, emptyBeam());
    return out.get(c);
  };
  for (const e of graph.edges) {
    if (e.kind === "reads") get(e.target).input.push(e.source);
    else if (e.kind === "writes") get(e.source).output.push(e.target);
    else if (e.kind === "runs_on") get(e.source).engine.push(e.target);
    else if (e.kind === "implements") get(e.target).package.push(e.source);
  }
  return out;
}

/*
 * Lay a workflow's steps out as a DAG. ``steps`` are step ids in the
 * workflow's own order, ``edges`` ``[{from, to}]`` over those ids.
 * Returns ``{pos: {id: {col, row}}, cols, rows}``. With no links the steps
 * sit on one row in order; a cycle (refused by the API, tolerated here)
 * puts the remaining steps one column after the rest.
 */
export function dagLayout(steps, edges) {
  const ids = [...new Set(steps)];
  const order = new Map(ids.map((id, i) => [id, i]));
  const links = (edges || []).filter((e) => order.has(e.from) && order.has(e.to) && e.from !== e.to);
  const pos = {};
  if (!links.length) {
    ids.forEach((id, i) => { pos[id] = { col: i, row: 0 }; });
    return { pos, cols: Math.max(1, ids.length), rows: 1 };
  }
  const preds = new Map(ids.map((id) => [id, []]));
  const succs = new Map(ids.map((id) => [id, []]));
  for (const e of links) { preds.get(e.to).push(e.from); succs.get(e.from).push(e.to); }
  // Longest path from a source (Kahn, ties in step order).
  const indeg = new Map(ids.map((id) => [id, preds.get(id).length]));
  const col = new Map();
  let ready = ids.filter((id) => indeg.get(id) === 0);
  const seen = new Set();
  while (ready.length) {
    ready.sort((a, b) => order.get(a) - order.get(b));
    const id = ready.shift();
    seen.add(id);
    const c = Math.max(-1, ...preds.get(id).filter((p) => col.has(p)).map((p) => col.get(p))) + 1;
    col.set(id, c);
    for (const s of succs.get(id)) {
      indeg.set(s, indeg.get(s) - 1);
      if (indeg.get(s) === 0) ready.push(s);
    }
  }
  const maxCol = Math.max(0, ...col.values());
  for (const id of ids) if (!seen.has(id)) col.set(id, maxCol + 1);
  // Rows: column by column, ordered by the mean row of the upstream steps.
  const byCol = new Map();
  for (const id of ids) {
    const c = col.get(id);
    if (!byCol.has(c)) byCol.set(c, []);
    byCol.get(c).push(id);
  }
  const row = new Map();
  for (const c of [...byCol.keys()].sort((a, b) => a - b)) {
    const bary = (id) => {
      const up = preds.get(id).filter((p) => row.has(p));
      return up.length ? up.reduce((a, p) => a + row.get(p), 0) / up.length : order.get(id);
    };
    byCol.get(c).sort((a, b) => bary(a) - bary(b) || order.get(a) - order.get(b)).forEach((id, r) => row.set(id, r));
  }
  for (const id of ids) pos[id] = { col: col.get(id), row: row.get(id) };
  return {
    pos,
    cols: Math.max(1, ...ids.map((id) => col.get(id) + 1)),
    rows: Math.max(1, ...[...byCol.values()].map((l) => l.length)),
  };
}

/*
 * One workflow step's beam: the config's beam with the step's address
 * overrides applied; a ``data`` link feeds the upstream step's output in
 * as this step's input (it wins over the override, as in the runner).
 */
function stepBeams(steps, edges, bm) {
  const out = {};
  for (const s of steps) {
    const base = bm.get(s.config) || emptyBeam();
    out[s.id] = {
      input: s.input_address ? ["address:" + s.input_address] : base.input.slice(),
      engine: base.engine.slice(),
      package: base.package.slice(),
      output: s.output_address ? ["address:" + s.output_address] : base.output.slice(),
    };
  }
  const fed = new Map();
  for (const e of edges) {
    if (e.type !== "data" || !out[e.from] || !out[e.to]) continue;
    if (!fed.has(e.to)) fed.set(e.to, []);
    fed.get(e.to).push(...out[e.from].output);
  }
  for (const [id, ins] of fed) if (ins.length) out[id].input = [...new Set(ins)];
  return out;
}

/*
 * Build the lane list (before ordering). ``layers`` is the set of shown
 * layers. Returns ``[{id, kind, group, label, head, weight, ...}]``:
 * ``head`` is the node id in the lane header; a workflow lane has
 * ``steps`` (``[{id, config, label, join}]``), ``edges``, ``beams`` (step
 * id → beam) and ``dag`` (``dagLayout``); a pipeline lane its ``beam``;
 * accounts / unused lanes the ``nodes`` they show.
 */
export function buildLanes(graph, { layers }) {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const bm = beams(graph);
  const lanes = [];
  const inWorkflow = new Set();
  const stepEdges = new Map();
  for (const e of graph.edges) if (e.kind === "step") {
    if (!stepEdges.has(e.source)) stepEdges.set(e.source, []);
    stepEdges.get(e.source).push(e.target);
  }
  for (const wf of graph.nodes.filter((n) => n.kind === "workflow").sort(byName)) {
    const m = wf.meta || {};
    // Steps: the workflow's own list (step ids), else the edge order.
    let steps = (m.steps || [])
      .map((s) => ({ id: s.id || s.pipeline, config: "config:" + s.pipeline, label: s.label || "", join: s.join || "all", input_address: s.input_address || null, output_address: s.output_address || null }))
      .filter((s) => byId.has(s.config));
    if (!steps.length) steps = [...new Set(stepEdges.get(wf.id) || [])].map((c) => ({ id: c, config: c, label: "", join: "all", input_address: null, output_address: null }));
    const known = new Set(steps.map((s) => s.id));
    const edges = (m.edges || []).filter((e) => known.has(e.from) && known.has(e.to)).map((e) => ({ from: e.from, to: e.to, type: e.type || "data", on: e.on || "success" }));
    for (const s of steps) inWorkflow.add(s.config);
    const dag = dagLayout(steps.map((s) => s.id), edges);
    lanes.push({ id: "lane:" + wf.id, kind: "workflow", group: "workflows", label: wf.name, head: wf.id, steps, edges, beams: stepBeams(steps, edges, bm), dag, weight: dag.rows });
  }
  for (const c of graph.nodes.filter((n) => n.kind === "config" && !inWorkflow.has(n.id)).sort(byName)) {
    lanes.push({ id: "lane:" + c.id, kind: "pipeline", group: "pipelines", label: c.name, head: c.id, beam: bm.get(c.id) || emptyBeam(), weight: 1 });
  }
  if (layers.has("storage")) {
    const accounts = graph.nodes.filter((n) => n.kind === "account").sort(byName).map((n) => n.id);
    if (accounts.length) lanes.push({ id: "lane:accounts", kind: "accounts", group: "accounts", label: "storage accounts", head: null, nodes: accounts, weight: 1 });
  }
  const used = new Set();
  for (const b of bm.values()) for (const ids of Object.values(b)) for (const id of ids) used.add(id);
  const kindRank = { package: 0, engine: 1 };
  const unused = graph.nodes
    .filter((n) => n.kind in kindRank && !used.has(n.id) && layers.has(n.layer))
    .sort((a, b) => (a.kind === b.kind ? byName(a, b) : kindRank[a.kind] - kindRank[b.kind]))
    .map((n) => n.id);
  if (unused.length) lanes.push({ id: "lane:unused", kind: "unused", group: "unused", label: "not used by a run config", head: null, nodes: unused, weight: 1 });
  return lanes;
}

/* The config ids a lane runs (for the "running" test). */
export function laneConfigs(lane) {
  if (lane.kind === "workflow") return [...new Set(lane.steps.map((s) => s.config))];
  if (lane.kind === "pipeline") return [lane.head];
  return [];
}

/*
 * Order lanes. With ``floatRunning`` every lane for which ``isLive(lane)``
 * is true moves to the top of its group; the relative order inside each
 * group is kept and the groups never mix.
 */
export function orderLanes(lanes, { floatRunning, isLive }) {
  if (!floatRunning) return lanes.slice();
  const out = [];
  for (const g of GROUPS) {
    const mine = lanes.filter((l) => (l.group || GROUP_OF[l.kind]) === g.key);
    out.push(...mine.filter(isLive), ...mine.filter((l) => !isLive(l)));
  }
  return out;
}

/* Split ordered lanes into ``[{key, label, lanes}]`` (empty groups dropped). */
export function groupLanes(lanes) {
  return GROUPS.map((g) => ({ ...g, lanes: lanes.filter((l) => (l.group || GROUP_OF[l.kind]) === g.key) })).filter((g) => g.lanes.length);
}

/*
 * Divide ``height`` px between lanes. ``weights`` is a lane count or the
 * per-lane weights (rows). ``groups`` group headers each take
 * ``groupHead`` px plus ``groupGap`` before every header but the first.
 * Returns ``{laneH, scroll, density}`` where ``laneH`` is the height of one
 * row: the lanes share the height exactly while each row stays at least
 * ``min`` tall; below that the lane list scrolls inside the canvas. The
 * canvas uses the same rule in CSS (``flex: <weight> 1 0`` +
 * ``min-height``); this picks the card density.
 */
export function fitLanes(weights, height, { min = LANE_MIN, gap = LANE_GAP, groups = 0, groupGap = GROUP_GAP, groupHead = GROUP_HEAD } = {}) {
  const ws = Array.isArray(weights) ? weights.map((w) => Math.max(1, w || 1)) : Array.from({ length: Math.max(1, weights) }, () => 1);
  const n = Math.max(1, ws.length);
  const total = ws.reduce((a, w) => a + w, 0) || 1;
  const fixed = gap * (n + groups + 1) + groups * groupHead + Math.max(0, groups - 1) * groupGap;
  const raw = (height - fixed) / total;
  const laneH = Math.max(min, Math.floor(raw));
  const scroll = raw < min;
  const density = laneH < COMPACT_BELOW ? "compact" : laneH >= ROOMY_FROM ? "roomy" : "normal";
  return { laneH, scroll, density };
}

/* Beam roles shown for the given layers. */
export function visibleColumns(layers) {
  return COLUMNS.filter((c) => layers.has(c.layer));
}
