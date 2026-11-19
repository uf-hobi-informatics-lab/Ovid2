---
title: Assistant & agents
summary: The optional in-panel LLM assistant and Agent Ovid over MCP — what they can see, what they can do, and how they stay safe.
audience: [user, developer, admin]
---

Ovid2 can work with AI in two ways. Both follow the same rule: **the AI
plans, the API authorises, a human approves anything consequential, and
only the executor touches data.**

| | In-panel assistant | Agent Ovid (MCP) |
| --- | --- | --- |
| Where | the robot button on the panel's right rail | any MCP client (desktop assistants, IDEs, agents) |
| Reads | GET-only API tools, with your role capped at operator | the 101 typed MCP tools |
| Acts | proposals, draft jobs and UI changes **you** click to run | submits, edits, cancels, behind RBAC and approvals |
| Models | yours: any OpenAI-compatible endpoint, plus an optional decision model | the client's model |
| Memory | saved **sessions** per user, searchable ([Sessions](sessions.md)) | the client's own |
| Guide | [Assistant tools](assistant-tools.md) | [Agent Ovid (MCP)](mcp.md) |

## Configure the assistant

1. **Configure → LLMs → + General LLM.** Set:
   - a name;
   - `base_url`, ending in `/v1` (OpenAI, vLLM, NIM, LiteLLM, Ollama, or
     your gateway);
   - `model`, exactly as `GET /v1/models` lists it;
   - `api_key`, ideally a service-account key scoped to this model.

   Optional fields: `timeout_s`, `max_tokens`, `temperature`,
   `context_window`, `system_prompt`, `ca_bundle`.
2. Click **Test connection**.
3. Optional: **+ System 1 LLM**. This is a fast decision model that is
   asked first. It answers with `{decision, confidence, action,
   rationale}`. If it isn't in scope, or its confidence is below the
   threshold, the general model answers instead.
4. In **Routing**, tick **Assistant enabled**, pick the model(s), and click
   **Save routing**. Tick **Allow the assistant to call Ovid2 API tools**
   to let it query Ovid2.

### Routing settings

All of these are on **Configure → LLMs → Routing** and are stored under
`llm` in `settings.json`.

| Setting | Default | What it does |
| --- | --- | --- |
| Assistant enabled | off | The kill switch. When it is off, the chat endpoint returns 404 and the panel says so. |
| System 1 (decides first) | none | Optional decision model, asked before the general model. |
| Confidence threshold | 0.7 | Below this, a System 1 decision is *deferred* and the general model answers. |
| Fall back to general | on | When off, a turn System 1 doesn't accept ends with an error instead of a general answer. |
| General LLM | none | The model that writes answers, calls tools and generates [session titles](sessions.md#session-titles). |
| Max concurrent chats | 4 | Upper limit on simultaneous chat turns across all users. Extra turns get *"The assistant is busy"*. |
| Turn timeout (s) | 180 | Wall-clock ceiling for one turn, tool calls included. The executor is killed when it is exceeded. |
| Allow API tools | off | Lets the general model call [assistant tools](assistant-tools.md). |
| Max tool steps | 6 | Model round-trips per turn when tools are on. |
| Tool result cap (chars) | 8000 | Each tool result is cut to this length before it reaches the model. |

A System 1 connection whose provider is still a stub (for example `jev`
before it ships) is skipped **silently**: the general model answers and
no notice is shown. A System 1 that is configured but *fails* (network,
auth) shows a one-line notice, then falls back.

### From the command line

The same executor the panel uses can be run directly. It is handy on a
server without a browser:

```bash
ovid2 tools test-llm nemotron-general          # authenticate + check the model; JSON result
echo '{"messages":[{"role":"user","content":"hi"}],
       "route":{"general":"nemotron-general"}}' | ovid2 tools llm-chat   # one turn, JSON lines
```

No endpoint, model or key ships as a default. You bring your own.

## Use it

Open the robot button on the right rail.

- The **header** has **history** and **search** buttons next to *Agent
  Ovid*, a **context meter**, and **New session**. See
  [Sessions, history & context](sessions.md).
- The **canvas** shows the conversation, Markdown, decision cards with a
  confidence bar, collapsible reasoning, and rich blocks (tables, KPI
  rows, status lists).
- In the **chat box**, <kbd>Enter</kbd> sends and
  <kbd>Shift</kbd>+<kbd>Enter</kbd> adds a new line. **Stop** cancels
  the turn.
- The **footer** names the routed models (*System 1: … · General: …*).
  Hover over an answer to see which model wrote it and how long it took.

Every conversation is saved as a session. Close the panel, move between
pages or come back tomorrow, and it is still there. Start a **new
session** when you change topic or when the context meter turns amber.

## Privacy

- API keys live in `secrets.d/llm_<name>.json`. The web server never reads
  them; each turn runs in a short-lived executor subprocess.
- Conversations are saved on the server as [sessions](sessions.md) (in
  the web database), owned by the signed-in user. **With sign-in off,
  everyone shares one history.** Users delete their own sessions;
  nothing expires automatically.
- The audit log records **metadata only**: actor, session id, model,
  token counts, latency, and tool names. It never records message text.
- Tool results (logs, configs, metadata) go to *your* model endpoint.
  Row-level data endpoints are excluded. Use an endpoint approved for that
  content.

> [!CAUTION]
> Don't paste PHI into the chat. Messages are stored in sessions and are
> sent to your model endpoint.
