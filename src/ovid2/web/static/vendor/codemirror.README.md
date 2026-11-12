# codemirror.bundle.js

Offline ES-module bundle of CodeMirror 6 for the pipeline builder's YAML
editor (no CDN; served from `/static/vendor/`). MIT-licensed (licence
texts appended at the end of the bundle by `--legal-comments=eof`).

Contents (exports): `EditorView`, `EditorState`, `Compartment`, keymaps,
`history`, `lineNumbers`, `foldGutter`, `bracketMatching`,
`syntaxHighlighting` / `HighlightStyle`, `yaml()`, `linter` /
`lintGutter` / `setDiagnostics`, `autocompletion`, `tags`.

Pinned versions: @codemirror/view 6.43.15, state 6.7.6, commands 6.11.1,
language 6.13.1, lang-yaml 6.1.3, lint 6.9.7, search 6.7.2,
autocomplete 6.20.4, @lezer/highlight 1.2.5, yaml 2.9.1 (ISC; `yamlParse` /
`yamlStringify` for the form ↔ YAML sync) — bundled with esbuild 0.24.2
(`esbuild entry.js --bundle --format=esm --minify --legal-comments=eof`).

Rebuild (no host node needed):

```sh
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -w /w node:20-alpine \
  sh -c "npm install @codemirror/view @codemirror/state @codemirror/commands \
  @codemirror/language @codemirror/lang-yaml @codemirror/lint @codemirror/search \
  @codemirror/autocomplete @lezer/highlight yaml@2 esbuild@0.24 && \
  npx esbuild entry.js --bundle --format=esm --minify --legal-comments=eof \
  --outfile=codemirror.bundle.js"
```

`entry.js` re-exports the names listed above; see
the adr-pipeline-builder design note.
