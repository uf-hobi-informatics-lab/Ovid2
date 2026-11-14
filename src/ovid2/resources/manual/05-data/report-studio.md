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
  out of the canvas and **every export**.
- **Canvas.** The report as it will be exported. Click a block to select it.
- **Inspector.** Settings for the report, the selected section or the
  selected block:
  - **Report** — subtitle, introduction (markdown), report theme, page size
    and orientation, table of contents, provenance footer, small-cell
    suppression.
  - **Section** — title, description, visible, start on a new page.
  - **Block** — dataset, chart kind (the gallery), options generated from
    the chart's parameters, filters, top-N, caption, width (full, two
    thirds, half, third).

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
| XLSX | one sheet per visible block's data plus a provenance sheet |
| SVG / PNG | every chart (zip), or one chart from the block inspector |
| Markdown | text plus figures (zip) |
| JSON | the layout definition, re-importable |

Hidden sections and blocks are never exported, and every export is audited.
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
| `add_section` | `title`, optional `index`, `section_id` |
| `update_section` | `section_id`, `set`: title, description, visible, page_break_before |
| `move_section` | `section_id`, `index` |
| `remove_section` | `section_id` |
| `add_block` | `section_id`, optional `index`, `block` |
| `update_block` | `block_id`, `set`, optional `merge_params` (a `null` value removes a key) |
| `move_block` | `block_id`, optional `section_id`, `index` (`-1` = last) |
| `remove_block` | `block_id` |
| `set_visibility` | `target_id` (section or block), `visible` |

A batch of up to 40 operations is applied atomically. If any one of them
fails, or the result fails validation, nothing changes.
