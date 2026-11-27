/*
 * Ovid Atlas — shared state store.
 *
 * One plain object every module reads; ``emit(topic)`` notifies the
 * subscribers (the canvas re-renders on "graph" / "view", the dock on
 * "select" / "live"). View state (layers, pinned nodes, "running on top",
 * dock size, overlays) persists per browser in localStorage and can be
 * saved as a *shared* workspace view (PUT /api/v1/atlas/layouts).
 *
 * v2 (lane board): the free-canvas camera (zoom / offsets / mode /
 * expanded) is gone; the key moved so a stale v1 camera never comes back.
 * Older shared views still load — their camera keys are ignored.
 */

const STORE_KEY = "ovid2.atlas.v2";
try { localStorage.removeItem("ovid2.atlas.v1"); } catch (_e) { /* storage disabled */ }

const saved = (() => {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch (_e) { return {}; }
})();

export const state = {
  graph: { nodes: [], edges: [], digest: "" },
  byId: new Map(),
  layers: new Set(saved.layers || ["algorithm", "compute", "storage"]),
  pinned: new Set(saved.pinned || []),
  floatRunning: saved.float_running !== false, // "running on top", on by default
  overlays: new Set(saved.overlays || ["presence"]),
  dock: Object.assign({ size: 300, side: "bottom", min: false, max: false }, saved.dock || {}),
  selected: null,
  hover: null,
  search: "",
  impact: null, // {root, nodes:Set, edges:Set}
  live: { jobs: [], workflow_runs: [], server_now_ms: 0 },
  liveByConfig: new Map(),
  replay: null, // {job, items, stages, t0, t1, t}
  heat: null,
  health: null,
  drift: null,
  presence: [],
  proposals: new Map(), // node id → [{name, method, path, body}]
  caps: { write: false, run: false, data: false },
  cache: { pkg: {}, table: {}, inspect: {}, node: {} },
  view: "",
};

const subs = new Map();

export function on(topic, fn) {
  if (!subs.has(topic)) subs.set(topic, []);
  subs.get(topic).push(fn);
}

export function emit(topic, detail) {
  for (const fn of subs.get(topic) || []) {
    try { fn(detail); } catch (err) { console.error("[atlas]", topic, err); } // eslint-disable-line no-console
  }
}

export function persist() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(viewState()));
  } catch (_e) { /* storage full / disabled */ }
}

/* The closed set of keys a saved view carries (atlas_state_svc.LAYOUT_KEYS). */
export function viewState() {
  return {
    layers: [...state.layers],
    pinned: [...state.pinned].slice(-200),
    float_running: !!state.floatRunning,
    overlays: [...state.overlays],
    dock: state.dock,
  };
}

export function applyView(v) {
  if (!v || typeof v !== "object") return;
  if (Array.isArray(v.layers)) state.layers = new Set(v.layers);
  if (Array.isArray(v.pinned)) state.pinned = new Set(v.pinned);
  if (typeof v.float_running === "boolean") state.floatRunning = v.float_running;
  if (Array.isArray(v.overlays)) state.overlays = new Set(v.overlays);
  if (v.dock && typeof v.dock === "object") state.dock = Object.assign(state.dock, v.dock);
}

export function setGraph(g) {
  state.graph = g;
  state.byId = new Map(g.nodes.map((n) => [n.id, n]));
}

export function edgesOf(id) {
  return state.graph.edges.filter((e) => e.source === id || e.target === id);
}

/* The config node a package / engine / address card should show live data of. */
export function configsUsing(id) {
  const out = [];
  for (const e of state.graph.edges) {
    if (e.kind === "implements" && e.source === id) out.push(e.target);
    if (e.kind === "runs_on" && e.target === id) out.push(e.source);
    if (e.kind === "reads" && e.source === id) out.push(e.target);
    if (e.kind === "writes" && e.target === id) out.push(e.source);
    if (e.kind === "step" && e.source === id) out.push(e.target);
  }
  return out;
}
