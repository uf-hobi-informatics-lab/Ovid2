---
title: Report Studio
summary: Organise, design, share and export research reports built from finished runs — with a chart library, report themes and the assistant as planner.
audience: [researcher, user]
---

The **Report Studio** (sidebar **Explore → Reports**, `/reports`) is where
you keep and design reports. A report is a *layout* of sections and blocks
(charts, KPI tiles, tables, text) drawn from one **report source**.

## Report sources

A report source holds the aggregated results of one finished run. It
contains small named datasets, never the raw tables. Sources are created
automatically when a job finishes, and they are stored durably, so they
survive a reboot even though run artifacts do not.

| Kind | Produced by | Datasets |
| --- | --- | --- |
| `health` | `health` / `dq_checks` runs | summary, status mix, per-part outcomes, checks, metrics, exceptions, per-table |
| `analysis` | `analysis` runs (`monthly_counts`, `distinct_values`) | monthly volumes, column profile, top values, table rows |
| `run_summary` | every run (event log) | stages, tables, fixes, timings |
| `health_trend` | **Build trend** on 2+ health sources | results run-over-run |

To add an older run, open the **Sources** tab and enter its instance id. You
can also press **Open in Report Studio** on a run's health report.

Columns that may carry raw source values (for example the analysis *top
values*) are tagged `row_values`. They are masked as `<redacted>` for users
without the *view pipeline data* capability and for the assistant.

## Creating a report

Choose **New report**, then pick a source and a starting point:

- a **recommended template**, such as *EDC submission summary*, *Data-quality
  deep dive*, *Monthly volume and value profile*, *Run performance* or
  *Refresh-over-refresh trend*;
- one of **your templates** (or a template shared with everyone);
- a **blank** report.

Give it a folder (for example `EDC/2026`) and tags to organise the library.
The library has *All*, *My reports*, *Shared with me*, *Favourites*
(the star) and *Archived* views, plus search and tag filters.

## Designing a report

The Studio has three panes:

- **Outline.** Drag a section or block (or use ↑ / ↓) to change the stacking
  order. The **eye** hides it. Hidden items stay in the layout but are left
  out of **every export**. Your private notes are listed with a lock.
- **Canvas.** The report as it will be exported, and the place you edit it:
  - **Click any text to edit it in place**: the report title, subtitle and
    introduction, section titles and descriptions, block titles and
    captions, text / callout / note bodies, info labels and values, and
    checklist items. Enter (Ctrl+Enter for multi-line text) saves and Esc
    cancels. Multi-line text shows a small markdown bar (bold, italic,
    heading, lists, quote, code, link).
  - **Select a block** to get a floating toolbar: move up / down (it moves
    into the neighbouring section at the ends), width, hide / show,
    include in exports, duplicate and remove.
  - **"+" points** between blocks and at the end of each section open the
    *Add block* menu at that position.
  - The **Continuous / Pages** switch shows the report as one sheet, or as
    pages at the report's page size and orientation with the PDF's margins
    and a page footer. Pages mode is close to the PDF export (fonts can
    shift a page break slightly) and stays fully editable. **Zoom** offers
    Fit / 75 / 100 / 125 %. Both choices are remembered in this browser.
  - **Hidden** shows hidden sections and blocks dimmed, so you can show
    them again from the canvas. **My notes** shows or hides your private
    notes.
  - Keys: Alt+↑ / Alt+↓ move, H hides, Ctrl+D duplicates, Delete removes,
    Esc deselects.
- **Inspector.** Settings for the selection, in tabs:
  - **Report** — subtitle, introduction (markdown), report theme, page size
    and orientation, table of contents, provenance footer, small-cell
    suppression.
  - **Content** — the selected section's or block's fields (title, width,
    visible, include in exports, text, rows, items, tone …).
  - **Format** — the **effective formatting** of the selection (theme,
    font, size, text / accent / fill colour) and per-component overrides:
    text colour, accent, background, alignment, text size, and for blocks
    emphasis (plain, card, outline, filled), border and padding. Colours
    are theme swatches (theme roles, the palette, status colours), which
    follow the report theme when you switch it, or a custom fixed colour.
    ↺ resets one setting to the theme; *Reset all formatting* clears them.
  - **Data** (charts) — dataset, chart kind (the gallery), options generated
    from the chart's parameters, filters, top-N.

### Free blocks, notes and additional information

The *Add block* menu offers **free content** that needs no dataset:

| Block | Use |
| --- | --- |
| Text | A markdown paragraph. |
| Callout | A highlighted message (info, success, warning, danger, note). |
| Note | A shared note, stamped with its author and the time of the last edit. |
| Info | Label / value rows, for example *Reviewer*, *Data steward*, *Sign-off date*. |
| Checklist | Tick-box action items. |
| Divider | A horizontal rule. |
| Spacer | Vertical space (small, medium, large). |

**Notes section** (outline) adds an *additional information* section,
styled apart from the data sections, for notes, methods, an appendix or a
sign-off. Any section can be turned into one, or back, from its Content tab.

Every free block has an **Include in exports** switch. Turn it off for a
working note that collaborators see in the Studio but that is left out of
PDF, Word, PowerPoint, Excel, HTML and Markdown exports.

**Private notes** (*Add block → Private note*) are visible **to you only**.
They are stored apart from the report, so they are never in its versions,
never shared and never seen by the assistant. Anyone who can view a report
can annotate it privately. A private note sits after a block. If that
block is removed, the note moves to *Unplaced notes* in the outline and is
not lost. A private note marked *Include in my exports* is added to **your**
exports when you tick that option in the export dialog. Nobody else's
exports ever contain it. *Make shared* moves a private note into the report
(press Save to keep it), and *Make private* on a shared note does the
reverse. Older versions in History still contain a note that was shared
before.

Saving a report as a **template** keeps free blocks as scaffolding. Info
values are cleared and checklist ticks reset, so a *Sign-off* section
starts empty in every report made from it.

Edits stay in your browser, with **Undo / Redo** (Ctrl+Z / Ctrl+Shift+Z),
until you press **Save** (Ctrl+S). Each save is a new **version**. If
someone else saved in the meantime, Save is refused with a conflict instead
of overwriting their work. **History** lists every version, and restoring
one saves it again as the newest version.

The **Autosave** switch in the toolbar saves your edits automatically a few
seconds after you stop editing, and again when you leave the page. Each
autosave is a version marked *Autosave* in History. Autosave pauses for the
page (until you reload) if someone else saved in the meantime or a save
fails. It also waits for you to press **Save** after the assistant has
edited the report, because the assistant never saves on its own. The
switch is off by default, and your choice is remembered in this browser.

### Chart library

Every chart is a *kind* plus parameters, never hand-written chart code. The
option form only offers columns of a suitable type, and every change is
validated against the source.

| Category | Kinds |
| --- | --- |
| Compare categories | bar, grouped bar, stacked bar, 100 % stacked bar, lollipop, dot plot, bullet |
| Change over time | line, area, stacked area, step line, slope, sparkline |
| Distributions | histogram, boxplot, strip plot, density |
| Relationships | scatter, bubble, heatmap, calendar heatmap |
| Part of a whole | pie, donut, part-to-whole bar |
| Single value | KPI tile, gauge, progress |
| Tables | data table (with status colouring) |

### Report themes

The report theme is **independent of the web page theme**. A report looks
the same on every user's screen and in every export, whatever page theme
they use. The themes are *Publication* (default), *Grayscale print*,
*Colour-blind safe* (Okabe–Ito), *Presentation dark*, *High contrast* and
*Journal serif*.

### Small-cell suppression

Set **Small-cell suppression** to, for example, `11` to hide every count
column (record counts) whose value lies between 1 and 10. The value is
blanked *before* any chart, table, KPI or export is drawn, and the
provenance footer records how many cells were suppressed.

## Sharing

Reports are **private** until you share them. Use **Share** to give a user
or everyone with a role **can view** or **can edit** access:

- viewers can open, render, duplicate and export the report;
- editors can also change and save it;
- only the owner can share, file, tag or archive it.

Every share and unshare is written to the audit log. Saving a report as a
**template** copies only the layout, so other users who apply it see their
own data. When sign-in is turned off, everyone is the same anonymous user
and sharing has no effect.

## Exporting

**Export** renders the report, including any unsaved edits, on the
server:

| Format | Contents |
| --- | --- |
| PDF | paginated document, vector charts, provenance |
| HTML | single self-contained file, interactive charts, works offline |
| DOCX | Word document with chart images and tables |
| PPTX | PowerPoint presentation (16:9): a title slide, one slide per row of blocks (charts resized to fit, KPIs as tiles, long tables continued over several slides), and a provenance slide. Colours and fonts follow the report theme, so *Presentation (dark)* gives a dark deck |
| XLSX | one sheet per visible block's data, a *Notes* sheet (notes, info rows, checklist items) and a provenance sheet |
| SVG / PNG | every chart (zip), or one chart from the block inspector |
| Markdown | text plus figures (zip) |
| JSON | the layout definition, re-importable |

Hidden sections and blocks, and blocks with *Include in exports* turned off, are never exported. Per-component formatting (colours, alignment, emphasis) is carried into PDF, Word, PowerPoint and HTML. Every export is audited. When you have private notes marked *Include in my exports*, the export dialog asks whether to add them.
A download link works only for the user who made the export, for one hour.

## Planning with the assistant

The assistant (Agent Ovid) is designed to be the main planner of reports.
Ask it, for example, *"make an EDC summary of last night's health run for
the steering committee, landscape, colour-blind safe"*. It then:

1. reads the sources (`list_report_sources`, `get_report_source`: columns,
   types and ranges, never rows) and the chart library
   (`get_report_catalogue`);
2. **drafts** a report (`create_report_draft`) and opens it in the Studio;
3. in the Studio, edits the open report with the `edit_report_layout`
   action. Every edit is validated by the same server validator, appears
   immediately and can be undone.

The assistant never saves, shares or exports on its own. Saving and
exporting appear as proposals you confirm, and sharing is human-only.

### Edit operations

Layouts change only through a small, closed set of operations. The editor,
the REST API (`POST /api/v1/reports/{id}/ops`) and the assistant all use
them:

| Op | Arguments |
| --- | --- |
| `set_report` | `set`: title, subtitle, description, theme, page, options |
| `add_section` | `title`, optional `index`, `section_id`, `kind` (`content` / `notes`) |
| `update_section` | `section_id`, `set`: title, description, visible, page_break_before, kind, style; optional `merge_style` |
| `move_section` | `section_id`, `index` |
| `remove_section` | `section_id` |
| `add_block` | `section_id`, optional `index`, `block` |
| `update_block` | `block_id`, `set`, optional `merge_params` / `merge_style` (a `null` value removes a key) |
| `move_block` | `block_id`, optional `section_id`, `index` (`-1` = last) |
| `remove_block` | `block_id` |
| `set_visibility` | `target_id` (section or block), `visible` |

A block's `style` holds the Format overrides (`accent`, `background`,
`text_color` as a theme role — `accent`, `text`, `muted`, `surface`,
`background`, `border`, `palette:N`, `status:<name>` — or `#rrggbb`;
`align`, `emphasis`, `font_scale`, `border`, `padding`). With `merge_style`
only the given keys change and `null` resets one to the theme. A block
with `export: false` stays in the Studio but is left out of every export
(the *Include in exports* switch). A section with `kind: notes` is an
*additional information* section. The server
stamps `author` / `updated_at` on note blocks; values sent by a client are
ignored. Private notes use their own endpoints
(`GET` / `POST /api/v1/reports/{id}/notes`, `PUT` / `DELETE
/api/v1/reports/{id}/notes/{note_id}`), which refuse the assistant.
They are merged into a render only for their author (`?private_notes=true`)
and into the author's own exports only with `include_private_notes`.

A batch of up to 40 operations is applied atomically. If any one of them
fails, or the result fails validation, nothing changes.
