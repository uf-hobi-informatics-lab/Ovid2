# docs/

**User-facing documentation lives in the Ovid2 Manual**, not here:

- Source: `src/ovid2/resources/manual/` (Markdown, ordered by `manual.yaml`).
- In the web panel: **System → Manual** (`/manual`).
- Static site: `ovid2 manual build --out site/` (or `ovid2 manual serve`).
- Authoring guide: the manual's *Developer guide → Writing documentation*.

This directory keeps design and background documents, diagrams, and
generators:

| File | Purpose |
| --- | --- |
| `diagrams/` | Architecture diagrams (Mermaid sources + PNG/SVG renders; SVGs are copied into the manual's `assets/`). |
| `formatter-spec.md`, `transforms.md`, `pipelines.md`, `engines.md`, `dataspace.md`, `auth.md`, `llm.md`, `installer.md`, `display-events.md`, `dev-instances.md`, `quickstart-local.md`, `parity.md` | Detailed subsystem notes. Their content is consolidated into the manual; keep them consistent when you change either. |
| `plan.md`, `legacy-parity-plan.md`, `pipeline-migration-guide.md` | Planning / migration notes. |
| `ovid2_architecture.pdf`, `generate_architecture_pdf.py` | Bundled architecture PDF and its generator. |
