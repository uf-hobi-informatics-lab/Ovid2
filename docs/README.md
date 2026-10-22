# docs/

Documentation lives here. During P1 only the plan itself is captured; each
phase adds the relevant reference document.

## Roadmap

| File | Phase | Purpose |
| --- | --- | --- |
| ``plan.md``                   | P1 (this) | Overall refactoring plan and phases. |
| ``architecture.md``           | P2        | Engine architecture, ABCs, lazy execution. |
| ``formatter-spec.md``         | P4        | Full formatter YAML reference + adapter catalog. |
| ``display-events.md``         | P3        | Event bus schema + renderer contracts. |
| ``installer.md``              | P6        | Installation, XDG paths, systemd deployment. |
| ``pipeline-migration-guide.md``| P7        | Step-by-step for porting a legacy pipeline. |
| ``pipelines.md``              | reference | Pipeline kinds; which scaffold a package vs. run package-free (health / copy / move). |
| ``ovid2_architecture.pdf``    | reference | Bundled architecture / features / roadmap reference PDF (with clickable ToC). Regenerate via ``python docs/generate_architecture_pdf.py``. |
| ``generate_architecture_pdf.py`` | tool   | Idempotent generator for the PDF above. Depends only on ``reportlab``. |
