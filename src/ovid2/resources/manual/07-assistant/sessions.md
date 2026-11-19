---
title: Sessions, history & context
summary: Every chat with the assistant is a saved session — reopen it from the history menu, search old conversations by keyword, watch the context meter, and start a fresh session when answers drift.
audience: [user, admin]
---

Every conversation with the in-panel assistant is saved as a
**session**: an ordered list of your messages and the assistant's
answers, plus a title and token counters. Sessions are stored on the
Ovid2 server, not in the browser. A chat therefore survives closing the
tab, reloading the page, opening another browser, and restarting the
server.

## The panel header

```text
┌────────────────────────────────────────────────────────────┐
│ 🤖 Agent Ovid   [⟲ history] [🔍 search]    ▬▬▭ 11.4k/128k  [＋] │
├────────────────────────────────────────────────────────────┤
│ Copy Run Kind Duplicates Ovid2 Data          ← session title │
├────────────────────────────────────────────────────────────┤
│  …conversation…                                              │
```

| Control | What it does |
| --- | --- |
| **History** (clock with an arrow) | Shows or hides the session list under the header. The icon is highlighted while the list is open. |
| **Search** (magnifier) | Opens the session list with the cursor in the search box. |
| **Context meter** | How much of the model's context window the latest turn used. See [The context meter](#the-context-meter). It is hidden until the session has had one answer. |
| **New session** (speech bubble with ＋) | Starts an empty session with a fresh context. The current session stays saved in the history. |
| **Session title** | The line under the header. It shows *italic* while the title is still being generated. |

## Starting and continuing a session

- **Type and send.** A new session is created by your first message.
  You don't have to press *New session* first.
- **Keep chatting.** Each message is added to the open session. Only
  your new message travels from the browser. The server adds the saved
  history (the most recent 40 messages) before it calls the model, so
  the model always sees the same conversation you see.
- **Navigate freely.** The open session follows you from page to page.
  Each tab remembers which session it has open. A new tab starts on an
  empty session, and the history menu reaches all of them.
- **Stop.** A turn you stop, or one that fails, is still saved. Its
  partial answer is marked *Stopped.*
- Results of **proposals**, **draft jobs** and **UI actions** are saved
  with the answer: whether they ran, were dismissed, were undone, or
  are awaiting approval. A reopened session shows them as you left
  them. Replaying a session never re-applies a UI action or re-runs a
  proposal.

## Session titles

After the **first answer** of a session, Ovid2 asks the routed
**general** model, in the background, for a short title of at most six
words, such as *"Three PCORnet CDM Tables"*.

- Until the title arrives, the start of your first message is shown in
  italics.
- If the model call fails or times out (45 s), the start of your first
  message (about 60 characters) becomes the title.
- **Rename** a session from the history list (✎). A name you choose is
  never overwritten by a generated one.
- The title call is a separate, small request to your model endpoint.
  It sends your first message and the start of the first answer, so it
  uses a few hundred tokens.

## The history menu

Click **History** to open the list of your sessions, newest first. Each
row shows:

- the **title**;
- **when** it was last used (*just now*, *5 min ago*, *2 d ago*, then
  the date);
- **how many messages** it has and **how much context** its latest
  turn used (for example `11.4k ctx`).

| Action | How |
| --- | --- |
| Open a session | Click the row, or focus it and press <kbd>Enter</kbd>. The list closes and the conversation loads. |
| Rename | ✎ on the row. Type a name, then <kbd>Enter</kbd> to save or <kbd>Esc</kbd> to cancel. Clicking elsewhere also saves. |
| Delete | ✕ on the row (or <kbd>Delete</kbd> while the row is focused), then confirm. Deletion is permanent. Deleting the open session leaves you on an empty one. |
| Close the list | Click **History** again, or press <kbd>Esc</kbd> in the search box. |

The open session is marked with an accent bar. On touch devices the ✎
and ✕ buttons are always visible. With a mouse they appear when you
hover over a row.

## Searching old conversations

Click **Search** (or open the history and type in the box). The list
filters as you type.

- **What is searched:** session titles **and** the text of every
  message, both yours and the assistant's.
- **All words must match.** `snowflake copy` finds sessions that
  mention both words, anywhere in the session.
- **Prefix matching.** `demo` matches *DEMOGRAPHIC* and *demo*.
- **Case-insensitive.** Punctuation and search operators you type are
  treated as plain text.
- Each hit shows a **snippet** of the message that matched, with your
  words highlighted. A match on the title alone has no snippet.
- Press <kbd>↓</kbd> to move from the search box into the results.

Clear the box to see every session again.

## The context meter

A language model only reads a fixed number of **tokens** per request:
its *context window*. Every turn sends:

- the system prompt;
- the tool catalogue (when tools are on);
- the saved conversation;
- your new message.

As a session grows, so does each request. Close to the window limit,
the model starts losing earlier details, contradicting itself or
repeating, even before older turns are trimmed. This is **context
rot**.

The meter makes it visible:

- **Number:** the tokens the latest turn put through the model (prompt +
  answer, using the largest step when the assistant called tools),
  against the routed general model's `context_window`. For example,
  `11.4k / 128k`.
- **Bar and colour:**

  | Colour | Context used | Meaning |
  | --- | --- | --- |
  | Green | under 60% | Plenty of room. |
  | Amber | 60–85% | Answers may start to drift. Consider a new session. |
  | Red | over 85% | Older turns are about to be trimmed. Start a new session. |

- **Tooltip** (hover over the meter): the exact counts, plus the
  session's running totals of prompt and completion tokens (what the
  endpoint billed).

> [!TIP]
> To carry on a long task in a fresh session, ask the assistant first
> to *"summarise what we decided so far in five bullets"*. Then press
> **New session** and paste the summary as your first message.

**Where the window comes from.** It is the `context_window` field of
the LLM connection the general route uses (**Configure → LLMs →
edit**). The default is `128000`. Set it to your model's real window so
the meter, and the trimming of very long conversations, are accurate.
The token counts are the ones your endpoint reports. If it doesn't
report usage, the meter stays hidden.

Even a brand-new session isn't at zero. With tools on, the tool
catalogue and system prompt alone are usually 8–12k tokens.

## Who can see my sessions

- Sessions belong to the signed-in person. Nobody else's list shows
  them, and opening someone else's session id returns *not found*.
- **While sign-in is off** (`settings.auth.enabled = false`, the
  default on a fresh install), everyone is the same `anonymous` user.
  **Everyone using the panel shares one history.** Enable sign-in
  ([Sign-in & roles](../08-operations/auth.md)) to give each person
  their own.
- The assistant itself cannot read sessions. Its per-turn API token,
  and the MCP service token, are refused. Sessions aren't offered as
  assistant or MCP tools.

## Privacy and retention

- Chat text is stored in the web database (`scheduler.db` under
  `$XDG_STATE_HOME/ovid2/`), alongside jobs and approvals. Treat that
  file as sensitive and back it up and restrict it like the rest of the
  state directory.
- The **audit log** still records **metadata only** for each turn:
  actor, session id, model, token counts, latency, and tool names. It
  never records message text.
- Sessions are kept **until the user deletes them**. Nothing expires
  automatically. For a retention policy, see
  [Maintenance](../08-operations/maintenance.md#assistant-sessions).

> [!CAUTION]
> Messages are stored and are sent to your model endpoint. Don't paste
> PHI into the chat unless the endpoint and this server are approved
> for it.

## API

The panel uses these routes, and scripts can use them too. They act
only on the caller's own sessions and need the `use_llm` capability.

| Method & path | Purpose |
| --- | --- |
| `GET /api/v1/llm/sessions?q=&limit=&offset=` | List sessions, newest first. `q` searches titles and messages and adds a `snippet`. |
| `POST /api/v1/llm/sessions` | Create an empty session (`{"title": ""}`; leave the title empty to have one generated). |
| `GET /api/v1/llm/sessions/{id}` | A session with its messages (`seq`, `role`, `content`, `meta`). |
| `PATCH /api/v1/llm/sessions/{id}` | Rename (`{"title": "…"}`). |
| `DELETE /api/v1/llm/sessions/{id}` | Delete the session and its messages. |
| `PUT /api/v1/llm/sessions/{id}/messages/{seq}/meta` | Store the final display state of an answer (used by the panel). |
| `GET /api/v1/llm/status` | Includes `context_window` for the meter. |

The chat stream (`POST /api/v1/llm/chat/stream`, not in the OpenAPI
schema) takes `{"messages": [<new message>], "session_id": "…"}`.
Without `session_id` it creates a session. The stream starts with a
`session_open` event, and ends with `done` followed by a `session`
event (id, title, token counters, `context_window`).

See the [API reference](../10-reference/api.md) for the schemas.
