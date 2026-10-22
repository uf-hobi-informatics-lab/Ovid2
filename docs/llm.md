# LLM assistant (general + System 1)

Ovid2 can use two kinds of large language model.

| Role | Purpose | Providers |
| --- | --- | --- |
| **System 1** | Fast decision model. It is asked first and returns `{decision, confidence, action, rationale}`. | `jev` (JEV or a similar decision model; stub provider until its wire protocol lands) or `openai_compatible` |
| **General** | Answers open questions that System 1 does not own. | `openai_compatible` — any OpenAI-compatible `/v1/chat/completions` endpoint |

Both slots are optional and independent: configure only a general model,
only a System 1 model, or both. No endpoint, model or key is shipped as a
default — you bring your own.

## Routing

```
user turn → System 1 configured? ─no──────────────────────────┐
               │yes                                           │
               ▼                                              │
          decide() ── error / not implemented ─► notice ──────┤
               │                                              │
     in_scope and confidence ≥ threshold?                     │
         yes → show decision card + answer (done)             │
         no  → decision card "deferred" ──────────────────────┤
                                                              ▼
                        fallback_to_general?  → stream the general LLM
```

System 1 `action`s are **displayed, never executed**. With **assistant
tools** off (the default) the general LLM is chat-only; see
[Assistant tools](#assistant-tools) to let it query and act on Ovid2.

## Configure (left menu → **Configure → LLMs**)

1. Click **+ General LLM** and fill in the form:

   | Field | Example |
   | --- | --- |
   | Name | `general` |
   | Role | General |
   | `base_url` | your endpoint root ending in `/v1` (e.g. `https://api.openai.com/v1`, `http://localhost:11434/v1` for Ollama, or your institution's AI gateway) |
   | `model` | the model id exactly as `GET /v1/models` lists it |
   | `api_key` (secret) | your endpoint key, ideally a **service-account** key scoped to this model |
   | `timeout_s` / `max_tokens` / `temperature` | optional (60 / 2048 / 0.2) |
   | `context_window` | optional (128000); older turns are trimmed to fit |
   | `system_prompt` | optional extra instructions |
   | `ca_bundle` | optional; only needed behind a TLS-intercepting proxy |

2. Click **Test connection**. This authenticates and confirms the model is
   visible to the key (`GET /v1/models`).
3. Click **+ System 1 LLM** and enter the decision model's `base_url`,
   optional `model`, `confidence_threshold` and `api_key`. Pick the `jev`
   provider for JEV-style decision endpoints, or `openai_compatible` to
   have a chat model emit strict-JSON decisions.
4. In the **Routing** card: tick **Assistant enabled**, pick the general
   LLM (and the System 1 model), set the threshold, then click **Save
   routing**.

Any OpenAI-compatible endpoint works with the `openai_compatible`
provider: vLLM, NIM, LiteLLM, Ollama or OpenAI. It can also fill the
System 1 role in the meantime by prompting it for strict-JSON decisions.

## Use (right rail → robot button)

The panel has two parts:

- A **canvas** showing chat turns, Markdown, decision cards with a
  confidence bar, and the model's collapsible "Reasoning".
- A **chat box** below it. Enter sends; Shift+Enter inserts a new line.
  **Stop** cancels the current turn and **clear** resets the
  conversation.

The model can emit canvas blocks as fenced `ovid-block` JSON:
`table`, `kpi-row` or `status-list`.

## Assistant tools

Turn on **Allow the assistant to call Ovid2 API tools** in the Routing
card (`settings.llm.tools_enabled`). The general LLM is then offered
function-calling tools that map one-to-one onto `/api/v1` routes. The
allow-list is `src/ovid2/resources/agent_tools/catalogue.yaml`; your
current list is at `GET /api/v1/llm/tools`.

```
model ─ tool call ─┬─ GET tool  → executor calls /api/v1/... with a per-turn
                   │              read-only token → result fed back → loop
                   ├─ POST tool → "Proposed action" card in the panel
                                  (nothing runs until YOU click Run)
                   └─ UI action → your browser re-checks + restyles the
                                  page at once; chip with Undo (no API call)
```

- **Read tools** (jobs, job logs, runs, timelines, health reports,
  pipeline configs and packages, formatter/mapper specs, addresses,
  connections, engines, data space). These run automatically inside the
  executor subprocess. The web server mints a short-lived bearer token
  for that turn. The token:
  - is **GET-only**: the API answers 403 to anything else, even while
    auth is disabled;
  - carries **your** role, **capped at pipeline operator**;
  - is attributed as `llm:<your user id>`;
  - is revoked when the turn ends.
- **Draft jobs** (`draft_pipeline_config` for a saved config,
  `create_job_draft` for an ad-hoc job). If you ask the assistant to run
  something, it prepares a **draft job**. A draft has every run field
  filled in (kind, pipeline, input/output, packages, engine, workers)
  but is not queued.

  Your browser creates the draft using your own session, with no click
  needed, and opens its run page at `/runs/<instance_id>`. The ▶ button
  there pulses to show the job is ready. Press it to queue the job
  (`POST /jobs/{id}/launch`), or click *discard* to delete the draft.

  The worker never runs a draft on its own. Drafts show up as `DRAFT` in
  the Jobs list. REST endpoints:
  - `POST /api/v1/job-drafts` creates a draft (same body as
    `POST /api/v1/runs`);
  - `POST /api/v1/jobs/{id}/launch` queues it;
  - `POST /api/v1/jobs/{id}/discard` deletes it.

  `POST /api/v1/pipelines/{name}/draft` drafts a run of a saved config.
  `POST /api/v1/pipelines/{name}/run` also accepts `input_address`,
  `output_address` (these bind an open side of the config) and
  `draft: true`.
- **Proposals** (`submit_run`, `cancel_job`,
  `rerun_job`, enable/disable a pipeline config). The executor never
  runs these. The panel shows the method, path and body. **Run** sends
  the request from your browser session, with your own permissions and
  any approval gate (a 202 shows *awaiting approval*). The outcome is
  then sent back so the assistant can follow up. **Dismiss** discards
  the proposal.
- **Appearance (UI actions)**: `set_appearance`, `save_appearance` and
  `apply_saved_appearance`. You can ask things like *"switch to the
  Tatooine theme with a red accent"*, *"make the text bigger and use a
  serif font"*, *"save this as desert"* or *"go back to desert"*.

  These tools never call the API. They change only how the page looks
  in **your** browser, using the same localStorage keys as the
  Appearance panel, so the change survives a reload. Your browser
  re-checks every value against its own allow-list:
  - known theme names only;
  - the Appearance panel's CSS variables only;
  - hex colours, `px`/`rem`/`em` sizes, `100`–`900` weights and plain
    font stacks.

  The change is applied at once, and a chip in the panel offers
  **Undo**. A change that would make body text unreadable (below 3:1
  contrast) is refused. Each turn sends your current theme, overrides
  and saved-theme names so the assistant knows the current look. Only
  the action *name* is audited.
- **Never exposed**: secrets, settings, install, users, approvals, LLM
  connections, storage browse, file preview, filesystem listing, and any
  DELETE. The contract test `tests/contract/test_agent_tools_catalogue.py`
  enforces this list. It also checks that every entry matches a real
  route and capability.
- Knobs: `max_tool_iterations` (default 6) bounds the number of model
  round-trips per turn; the final round-trip is made without tools.
  `tool_result_max_chars` (default 8000) caps each tool result fed to
  the model. Job logs keep the *tail*.
- The executor calls back on the socket the server listens on, not
  through the reverse proxy. Override with `OVID2_LLM_TOOLS_API_URL`.
- Tool results go to the configured LLM endpoint. Row-level data
  endpoints are excluded for that reason, but logs and configs are
  sent, so use a gateway approved for that content.

## Security model

- API keys live in `secrets.d/llm_<name>.json` (mode 0600). The web server
  never reads them. Each probe and each chat turn runs in a short-lived
  `python -m ovid2.llm` executor subprocess with an allow-listed
  environment.
- The conversation is held in the browser (`sessionStorage`). The server
  writes **metadata-only** audit rows (`llm.chat`): actor, route,
  connection, model, token counts, latency and outcome. **No prompt or
  answer text is stored.** Do not paste PHI into the assistant. With
  tools on, the audit row also lists every tool call (name, HTTP status,
  latency) and every proposal (name, method, path). Tool results are not
  stored.
- `use_llm` (viewer and above) is required to chat. `manage_config` is
  required to configure LLMs. Deleting a connection is approval-gated and
  refused while routing points at it.

## CLI

```bash
ovid2 tools test-llm general
echo '{"messages":[{"role":"user","content":"hi"}],"route":{"general":"general"}}' \
  | ovid2 tools llm-chat
```

## Adding a provider

1. Implement `decide()` (System 1) and/or `chat_stream()` (general) in
   `src/ovid2/llm/providers/<name>.py`.
2. Register it in `PROVIDER_TYPE_MAP`, and add the field spec and roles to
   `src/ovid2/config/llms.py`.
3. `tests/contract/test_llm_tier_isolation.py` keeps the catalogues in
   sync.

The `jev` provider is the System 1 slot's reference stub: implementing
its `decide()` against a JEV (or similar) decision endpoint is the only
change needed to light it up.
