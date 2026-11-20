# MCP Server: Analysis and Control from Claude Code — Design

Date: 2026-11-19

## Context

The harness records scope captures, analyzes them into ranked findings, and keeps each session as a case: a vehicle profile plus an append-only event log, OBD reads and a hand-off report. All of it is reachable only through the Streamlit laptop page, the phone remote and the CLI. The owner wants to talk through results with Claude Code (or any MCP client): why capture 004 looks worse than 003, what the 5V sag evidence says, what to try next, and to have the assistant take part in the session by arming a capture or reading the vehicle.

Relevant existing structure:

- `crank.controller.get_controller()` is a process-wide singleton, already shared by the laptop and phone pages, with a capture lock and an OBD lock. It holds the scope connection, the held OBD adapter and the running jobs, so anything that wants to arm or read must live in the same process.
- `crank/analysis.py`, `case.py`, `summary.py`, `render.py` and `store.py` are pure or file-based and have no UI dependency.
- `app.py` pushes the laptop's choices (mode, scenario, session, settings, thresholds) into the controller on every Streamlit rerun.
- The OBD transport already refuses everything except read-only requests. The app listens on `0.0.0.0` with no password (documented in the README).
- `Event` has `id, ts, type, text, data, refs`; `Event.from_dict` ignores unknown keys.

## Purpose and success

An MCP client on the same machine can inspect and analyze every saved session, and, when the owner allows it, arm captures, read the vehicle and add to the case, with everything the assistant did visible and labeled.

**Success** (all against the simulator and fakes, no hardware):

- The app starts an MCP endpoint on `127.0.0.1` automatically; `claude mcp add --transport http` with the URL shown in the sidebar connects and lists the tools.
- Read tools return verdicts, findings with evidence, comparisons, downsampled signal windows, case summaries and reports, with no controller locks held.
- Control and write tools do nothing unless **Allow AI control** is on, and say so clearly when it is off.
- An AI-armed capture shows on the laptop and phone exactly like one armed there, with an `[AI]` label. Case events the assistant adds carry `source: "mcp"` and are marked as AI-authored in the timeline and report.
- Existing tests, case files and reports behave as before.

## Out of scope

- LAN access and tokens. The endpoint is localhost only. Opening it up is a later, separate decision.
- Connecting the scope (stays a laptop button: USB, self-check), raw SCPI or OBD commands, deleting sessions, and changing the laptop's mode, scenario, session or thresholds.
- MCP resources and prompts (tools only; a "diagnose this session" prompt is an easy follow-up).
- Any change to analysis thresholds or verdict logic, and clearing trouble codes.
- Validation against real hardware beyond what the layers underneath already have (#3).

## Design

### Components

**`crank/mcp_server.py`** (new, no UI code). Registers the tools on an `MCPServer` (the official `mcp` SDK 2.x, `from mcp.server.mcpserver import MCPServer`; streamable HTTP, path `/mcp`). The tools themselves are the methods of one `Tools` class in **`crank/mcp_tools.py`**, written over the shared `Controller` and the existing pure modules; tool failures are the SDK's `ToolError` (a plain exception loses its message over the protocol). `mcp_server.py` exposes:

- `build_server(ctrl) -> MCPServer`: registers `Tools(ctrl)`'s methods. The methods can be called directly in tests.
- `ensure_mcp_server(ctrl, port=None) -> McpStatus`: starts the server once per process and returns its status (`running`, `port_busy`, `error`, with the URL). The handle lives on `Controller.mcp` (so a module reload cannot lose it) and a lock makes repeated calls from Streamlit reruns no-ops. `CRANK_MCP_PORT=0` means a free port (used by tests), `stop_mcp_server(ctrl)` stops it, and `reset_controller()` stops it too. The server runs in a daemon thread with uvicorn bound to `127.0.0.1`, port from `CRANK_MCP_PORT` (default 8765). If the port is taken it returns `port_busy` and the app carries on.

**`app.py`.** Calls `ensure_mcp_server(ctrl)` after `get_controller()` and shows an "AI access" sidebar block: status, the URL, the copyable `claude mcp add --transport http ls-swap <url>` line, and the **Allow AI control** toggle (default off). The toggle writes to `ctrl.ai_control` only when the user flips it (`on_change`), so a stale second tab cannot turn control back on.

**`Controller`.** Gains `ai_control: bool = False`. No other behavior changes; the MCP layer calls the existing public methods (`arm`, `cancel`, `connect`, `disconnect`, `relink`, `start_obd`, `cancel_obd`, `stop_obd`, `snapshot`, `connection_status`, `case`).

**`crank/case.py`.** `Event` gains an optional `source: str = ""`, omitted from the JSON when empty (so existing logs and tests are unchanged, and `from_dict` already tolerates it). `Case.append` and `Case.set_hypothesis` gain an optional `source` argument. `crank/summary.py` and `crank/render.py` show a small "AI" marker and an "AI-authored" label for events with a source.

### Tools

Waveforms are never returned raw. All tools return compact JSON; failures are MCP tool errors carrying the same text the UI shows.

**Read (always available while the server is up)**

| Tool | Backed by | Returns |
|---|---|---|
| `list_sessions` | `SessionStore.list_sessions` | names, which is current |
| `get_case_summary(session?)` | `build_summary` | vehicle, suspects board, timeline, latest OBD read |
| `get_case_report(session?, format="markdown")` | `render.to_markdown` | hand-off report text |
| `list_captures(session?)` | `Session.list_captures` | id, label, time, simulated flag, verdict headline |
| `get_capture_analysis(capture_id, session?)` | `load_capture` + `analysis` | ranked findings with evidence, summary numbers, event table, thresholds used |
| `get_capture_signal(capture_id, channel, start?, end?, max_points=2000)` | `CaptureData` | downsampled window of one channel using the existing `report.decimate` min/max envelope. Window and point count are capped (the limits are in the error text), and `nan_samples` counts invalid samples. |
| `compare_captures(a, b, session?)` | both analyses | findings and key numbers side by side, differences called out |
| `reanalyze_capture(capture_id, thresholds?, session?)` | `analysis` | verdict under different thresholds. Not saved; a what-if. |
| `get_status` | `snapshot`, `connection_status`, OBD job | mode, scenario, current session, capture and OBD job state, scope and adapter state, whether AI control is on |
| `get_obd_log(lines=100, session?)` | `trafficlog` | tail of the adapter log |

**Progress (always available, so a result stays readable after the toggle goes off)**

| Tool | Backed by | Returns |
|---|---|---|
| `get_capture_progress` | `Controller.snapshot` | armed, capturing, downloading, or done with the new capture id and findings |
| `obd_progress` | the OBD job | state, seconds, message; for a finished read, a pointer to `get_case_summary` |

**Control and write (require Allow AI control)**

| Tool | Backed by | Notes |
|---|---|---|
| `arm_capture(label?, note?)` | `Controller.arm` | Returns at once ("armed, waiting for the crank"). Label is prefixed `[AI]`. Uses the laptop's current session, mode, scenario and settings. |
| `cancel_capture` | `Controller.cancel` | |
| `obd_connect(source)`, `obd_disconnect`, `obd_relink` | `connect`, `disconnect`, `relink` | `source` is `sim:<scenario>` or `serial:<port\|auto>`, as the UI already does |
| `obd_read(source)` | `start_obd` | Starts the background read and returns. Same read-only allowlist and real-vs-simulated case check as the UI. |
| `obd_stop`, `obd_cancel` | job methods | Stop keeps partial results; cancel records nothing |
| `add_case_note(type, text)` | `Case.append(source="mcp")` | `type` limited to observation, symptom and change. Current session. |
| `set_suspect(title, status, reason?)` | `Case.set_hypothesis(source="mcp")` | status open, ruled_out or confirmed. Matches an existing suspect by title (case-insensitive) and updates it, otherwise adds one. Current session. |

There are no edit or delete tools: AI entries are additions to the append-only log and cannot rewrite the owner's record. The control tools take no `session` argument because `Controller.arm` and `start_obd` always act on the controller's current session. The tool descriptions state this, and that the crank itself is always the owner's job.

The tool descriptions repeat the project's own rules: a verdict is what the harness reported, not a diagnosis; simulated data is labeled as such; "insufficient data" is a valid answer.

### Gating and attribution

- Each gated tool checks `ctrl.ai_control` first and returns "AI control is off. Enable it in the sidebar." when it is not set. Turning the toggle off mid-capture does not cancel the capture; it blocks new control calls only, and the message says so.
- The toggle is a plain flag. It starts off every time the app starts and is not persisted.
- AI-armed captures get a label starting `[AI]`. Notes and suspects get `source: "mcp"`. The timeline shows an "AI" marker, and the report labels those entries AI-authored, so they cannot be mistaken for the owner's observations.

### Session handling

The laptop's UI selections remain the source of truth, so the sync overwrite on rerun cannot clash with the assistant. Read tools take an optional `session` and default to the current one. The assistant never changes the laptop's mode, scenario, session or thresholds.

### Concurrency

The MCP thread is a third caller of the same controller code the laptop and phone already share, using the existing capture and OBD locks. Read tools read from disk and hold no controller locks, so a long analysis cannot block the UI. Arming returns immediately and the assistant polls, so no MCP request is held open for the length of a crank wait.

### Errors

Controller messages and `ObdError` subclasses become tool errors with their existing text. An unknown session or capture id lists the valid ones. An unknown channel or an over-large signal window states the limits. A busy port at startup degrades to a sidebar status.

## Testing

All without hardware, in the style of the existing suite.

- **Tool tests** call the tool functions directly on a `Controller` over a temp store and the simulator: each read tool's output, gating of every control and write tool with the toggle off and on, error messages, the `[AI]` label and `source` field, and that `reanalyze_capture` saves nothing.
- **Integration test** starts the real server on an ephemeral port, connects with the SDK's client, lists tools, and runs a simulator flow: arm, poll to done, fetch the analysis, compare two captures.
- **Lifecycle test:** `ensure_mcp_server` called twice starts one server; a busy port returns `port_busy` without raising.
- **Case tests:** `source` round-trips; events without it serialize exactly as before; legacy logs load unchanged; the summary and report mark AI-authored entries.
- Existing tests are unchanged and must still pass.

## Documentation

A README section: the sidebar block, the `claude mcp add` line, the tool table, the localhost-only and toggle rules, and a caveat that the control tools are not checked with a real scope and adapter (#3). The code layout list gains `crank/mcp_server.py`. `requirements.txt` gains `mcp>=2.3,<3`.
