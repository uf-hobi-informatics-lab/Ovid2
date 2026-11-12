---
title: Assistant tools
summary: What the assistant may read, propose, draft and restyle — and what it can never touch.
audience: [user, developer]
---

With tools enabled (`settings.llm.tools_enabled`), the general model can
call tools that map one-to-one to `/api/v1` routes. The allow-list is
`src/ovid2/resources/agent_tools/catalogue.yaml`. Your current list is at
`GET /api/v1/llm/tools`.

```text
model ─ tool call ─┬─ GET tool   → executor calls /api/v1/… with a per-turn read-only token
                   │               → result fed back to the model → loop
                   ├─ draft tool → a DRAFT job opens in your browser; ▶ to queue it
                   ├─ proposal   → a "Proposed action" card; nothing runs until YOU click Run
                   └─ UI action  → your browser re-checks and applies a cosmetic change, with Undo
```

## Read tools

The assistant can read jobs, job logs, runs, timelines, health reports,
pipeline configs and packages, formatter and mapper specs, addresses,
connections, engines and the Data Space. These run automatically with a
**per-turn token**. The token:

- is **GET-only** (the API refuses anything else, even with auth off);
- carries your role, **capped at pipeline operator**;
- is attributed as `llm:<your user id>`;
- is revoked when the turn ends.

## Draft jobs

Ask *"run the weekly health check on CHAR_DEMO"* and the assistant
prepares a **draft**: a complete job (kind, pipeline, input, output,
packages, engine, workers) that is **not queued**. Your browser opens the
draft's run page, where ▶ pulses. Press ▶ to queue it, or **discard** to
delete it. The worker never runs a draft by itself.

Ask *"build an EDC summary of the last health run"* and the assistant drafts
a **report** instead (`create_report_draft`). It reads the source's columns
and the chart library first, then opens the private draft in the
[Report Studio](../05-data/report-studio.md). Nothing is shared or exported.

## Proposals

For `submit_run`, `cancel_job`, `rerun_job`, and enabling or disabling
configs, the assistant shows a card with the method, path and body.
**Run** sends the request from **your** browser session, with your
permissions and any approval gate. **Dismiss** discards it.

## UI actions

`set_appearance`, `save_appearance` and `apply_saved_appearance` let you
say *"use the dark theme with a teal accent and save it as night shift"*.
Your browser:

- re-validates every value (known themes only, allowed variables only,
  safe colours, sizes, weights and fonts);
- refuses unreadable contrast;
- applies the change and shows **Undo**.

`edit_report_layout` changes the report open in the
[Report Studio](../05-data/report-studio.md), for example *"hide the status
section, move exceptions first and switch to the colour-blind theme"*. The
system prompt carries the report outline (ids only, sanitised). Your browser
sends the ops to the same server validator the editor uses, applies them to
your **unsaved** working copy, and shows **Undo**. Only you can **Save**.

## Never exposed

Secrets, settings, install, users, approvals, LLM connections, storage
browse, file preview, filesystem listing, and **any DELETE**. A contract
test checks the catalogue against this list on every commit.

## Limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_tool_iterations` | 6 | model round-trips per turn. The last one runs without tools. |
| `tool_result_max_chars` | 8000 | cap on each tool result; job logs keep the tail |
| `OVID2_LLM_TOOLS_API_URL` | the server's own socket | where the executor calls back |
