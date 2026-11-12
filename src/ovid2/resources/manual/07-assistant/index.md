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
| Reads | GET-only API tools, with your role capped at operator | the 100 typed MCP tools |
| Acts | proposals, draft jobs and UI changes **you** click to run | submits, edits, cancels, behind RBAC and approvals |
| Models | yours: any OpenAI-compatible endpoint, plus an optional decision model | the client's model |
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

No endpoint, model or key ships as a default. You bring your own.

## Use it

Open the robot button on the right rail.

- The **canvas** shows the conversation, Markdown, decision cards with a
  confidence bar, collapsible reasoning, and rich blocks (tables, KPI
  rows, status lists).
- In the **chat box**, <kbd>Enter</kbd> sends and
  <kbd>Shift</kbd>+<kbd>Enter</kbd> adds a new line. **Stop** cancels the
  turn; **clear** resets the conversation.

## Privacy

- API keys live in `secrets.d/llm_<name>.json`. The web server never reads
  them; each turn runs in a short-lived executor subprocess.
- The conversation stays in your browser. The server audits **metadata
  only**: actor, model, token counts, latency, and tool names.
- Tool results (logs, configs, metadata) go to *your* model endpoint.
  Row-level data endpoints are excluded. Use an endpoint approved for that
  content.

> [!CAUTION]
> Don't paste PHI into the chat.
