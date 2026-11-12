/*
 * Ovid2 YAML editor — a thin, themed wrapper over the vendored CodeMirror 6
 * bundle (/static/vendor/codemirror.bundle.js).
 *
 *   const ed = mountYamlEditor(textarea, {
 *     validate: async (text) => [{line, message, severity}],   // optional
 *     onChange: (text) => {},                                  // optional
 *   });
 *   ed.getValue(); ed.setValue(text); ed.focusLine(n);
 *
 * Progressive enhancement: the original <textarea> stays in the form (it
 * is hidden and kept in sync), so a plain form POST still carries the
 * text, and without JS the textarea is the editor. Colours come from the
 * app's CSS custom properties, so every theme / personality applies.
 */

import {
  EditorView, EditorState, keymap, lineNumbers, highlightActiveLine,
  highlightActiveLineGutter, drawSelection, defaultKeymap, history,
  historyKeymap, indentWithTab, indentOnInput, bracketMatching, foldGutter,
  syntaxHighlighting, HighlightStyle, yaml, linter, lintGutter,
  searchKeymap, highlightSelectionMatches, tags, yamlParse,
} from "/static/vendor/codemirror.bundle.js";

export { yamlParse };
export { yamlStringify } from "/static/vendor/codemirror.bundle.js";

const theme = EditorView.theme({
  "&": {
    fontSize: "13px",
    color: "var(--fg)",
    backgroundColor: "var(--bg)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius-md)",
  },
  "&.cm-focused": {
    outline: "none",
    borderColor: "var(--accent)",
    boxShadow: "0 0 0 3px var(--accent-subtle)",
  },
  ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "1.55" },
  ".cm-content": { caretColor: "var(--accent)", padding: "0.4rem 0" },
  ".cm-gutters": {
    backgroundColor: "var(--bg-elev)",
    color: "var(--fg-subtle)",
    borderRight: "1px solid var(--border)",
  },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--accent) 6%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "color-mix(in srgb, var(--accent) 10%, transparent)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in srgb, var(--accent) 25%, transparent) !important",
  },
  ".cm-tooltip": {
    backgroundColor: "var(--card-bg)",
    color: "var(--fg)",
    border: "1px solid var(--border)",
  },
  ".cm-diagnostic-error": { borderLeftColor: "var(--err)" },
  ".cm-diagnostic-warning": { borderLeftColor: "var(--warn)" },
});

const highlight = HighlightStyle.define([
  { tag: [tags.propertyName, tags.definition(tags.propertyName)], color: "var(--accent)", fontWeight: "600" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--ok)" },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: "var(--warn)" },
  { tag: tags.comment, color: "var(--fg-subtle)", fontStyle: "italic" },
  { tag: [tags.keyword, tags.operator, tags.punctuation, tags.separator], color: "var(--fg-muted)" },
  { tag: [tags.typeName, tags.labelName, tags.meta], color: "var(--info)" },
]);

/* Map a YAML parse error to a CodeMirror diagnostic. */
function parseDiagnostics(text, doc) {
  try {
    yamlParse(text);
    return [];
  } catch (err) {
    const pos = err && err.linePos && err.linePos[0];
    const line = pos ? Math.min(Math.max(pos.line, 1), doc.lines) : 1;
    const ln = doc.line(line);
    return [{ from: ln.from, to: ln.to, severity: "error", message: String(err.message || err).split("\n")[0] }];
  }
}

/* Best-effort: find the line a server error path (``steps/2/expr``) points at. */
function lineForPath(doc, path) {
  const parts = String(path || "").split("/").filter(Boolean);
  if (!parts.length) return 1;
  const key = [...parts].reverse().find((p) => !/^\d+$/.test(p) && p !== "<root>");
  if (!key) return 1;
  for (let i = 1; i <= doc.lines; i++) {
    if (new RegExp("^\\s*-?\\s*" + key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*:").test(doc.line(i).text)) return i;
  }
  return 1;
}

export function mountYamlEditor(textarea, opts = {}) {
  if (!textarea) return null;
  const host = document.createElement("div");
  host.className = "yaml-editor";
  textarea.insertAdjacentElement("afterend", host);
  textarea.hidden = true;
  let suppress = false;

  const serverLint = linter(async (view) => {
    const text = view.state.doc.toString();
    const local = parseDiagnostics(text, view.state.doc);
    if (local.length || !opts.validate) return local;
    let issues = [];
    try { issues = (await opts.validate(text)) || []; } catch (_e) { issues = []; }
    return issues.map((i) => {
      const line = i.line || lineForPath(view.state.doc, i.path);
      const ln = view.state.doc.line(Math.min(Math.max(line, 1), view.state.doc.lines));
      return { from: ln.from, to: ln.to, severity: i.severity || "error", message: i.message };
    });
  }, { delay: 600 });

  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: textarea.value,
      extensions: [
        lineNumbers(), highlightActiveLineGutter(), foldGutter(), drawSelection(),
        history(), indentOnInput(), bracketMatching(), highlightActiveLine(),
        highlightSelectionMatches(), yaml(), syntaxHighlighting(highlight), theme,
        lintGutter(), serverLint,
        EditorState.tabSize.of(2),
        keymap.of([indentWithTab, ...defaultKeymap, ...historyKeymap, ...searchKeymap]),
        EditorView.updateListener.of((u) => {
          if (!u.docChanged) return;
          const text = u.state.doc.toString();
          textarea.value = text;
          if (!suppress && opts.onChange) opts.onChange(text);
        }),
        EditorView.lineWrapping,
      ],
    }),
  });

  return {
    view,
    getValue: () => view.state.doc.toString(),
    setValue(text) {
      suppress = true;
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
      textarea.value = text;
      suppress = false;
    },
    focusLine(n) {
      const ln = view.state.doc.line(Math.min(Math.max(n, 1), view.state.doc.lines));
      view.dispatch({ selection: { anchor: ln.from }, scrollIntoView: true });
      view.focus();
    },
  };
}
