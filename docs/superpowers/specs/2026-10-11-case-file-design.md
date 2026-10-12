# Case File and Escalation Summary — Design

Date: 2026-10-11

## Context and roadmap

The crank-test harness is being grown into a general electrical-diagnosis workbench for one user (the owner) working on their own vehicles, running locally. The full idea splits into four independent pieces, each with its own spec, plan and implementation:

1. **Case file and escalation summary (this spec).** One case per vehicle problem, holding everything tried and collected, with a hand-off report for a mechanic or specialist.
2. **Bluetooth OBD-II reader.** Trouble codes, freeze frame and live data, appended to the case as `code_read` events.
3. **General test modules.** The crank analysis becomes the first of several modules (voltage drop, parasitic draw, alternator output, injector or ignition timing, CAN health).
4. **Chat assistant with retrieval.** Answers from retrieved forums, manuals and TSBs, using the case as context.

Order: 1, 2, 3, 4. This piece is the foundation: later pieces only append events to a case and read the summary.

## Purpose

On a new electrical problem the user starts a case, collects data from the scope (and later a code reader), and always knows what has been tried and ruled out. If they hand the problem to a specialist, the specialist reads one page and does not repeat the user's work.

**Success:** from a case with real history, one button produces a report (Markdown, and a shareable HTML page) that states the vehicle and problem, what was ruled out and why, what is still open, the key data, and the timeline, using only what was recorded.

## Constraints

- Single user, local, file-based. No server database, no accounts, nothing uploaded by the app.
- Builds on the existing code. Existing sessions open as cases without migration or rewriting; the existing 99 tests stay green.
- A case with missing information must say "not recorded", never invent content.

## Scope

In: vehicle profile, append-only event log, Case page (quick add, hypotheses board, timeline, report), automatic events from capture and guided tests, before/after linking in Compare, minimal phone quick-add, summary builder, Markdown and HTML renderers.

Out: Bluetooth code reader, other test modules, chat and retrieval, PDF output, cross-case search.

## Data model

A case is a session folder with two new files beside the existing `captures/`, `readings.json` and `notes.json`.

`vehicle.json` (all fields optional): year, make, model, engine, transmission, mileage, modifications (free text), VIN (optional), and a one-paragraph problem statement.

`events.jsonl`: append-only, one JSON object per line. Each event has `id` (e.g. `e0007`), `ts` (ISO timestamp), `type`, `text`, a type-specific `data` object, and `refs` pointing at files in the case (for example `{"capture": "003"}`).

| Type | `data` |
|---|---|
| `symptom` | what the vehicle does wrong, and when |
| `observation` | anything noticed that is not a measurement |
| `code_read` | `codes`: list of code, description, status; optional freeze frame; `source` is `manual` or `obd` |
| `measurement` | name, numeric value, unit, where, conditions (key off, cranking, hot) |
| `capture` | label plus a frozen snapshot of the verdict codes and key numbers at capture time; `sim` flag |
| `change` | what was done (re-grounded, part replaced, wire repaired) and the result |
| `hypothesis` | `hid`, title, status in {open, ruled_out, confirmed}, reason; the latest event for a `hid` wins |

Rules:
- Append-only. A correction is a new event.
- A damaged line is skipped and counted; the rest of the log still loads.
- A `capture` event stores the verdict at the time, so reports do not depend on later analysis changes.
- Unknown event types and extra fields load without error, so later pieces can add types that older code still reads.
- Legacy sessions: captures, `readings.json` and `notes.json` without events are read as synthesized events when the summary is built. No files are rewritten.

## Summary

`build_summary(case) -> Summary` is a pure function over the profile, event log and legacy files. It does no I/O and no formatting. A `Summary` holds:

- vehicle and problem statement;
- symptoms, newest first;
- timeline: all events in order, each with a one-line description;
- ruled out: hypotheses with status `ruled_out`, each with its reason and evidence refs;
- open and confirmed hypotheses;
- key data: latest value per named measurement, currently active codes, and per capture its label, time, verdict and headline numbers; a before/after pair when a `change` event separates two captures;
- not yet tried: guided-test steps with no saved result;
- attachments: capture files and CSVs by name.

Principles: only recorded content appears; gaps read "not recorded"; simulator captures are labeled simulated; a verdict is presented as "the harness reported X, with this evidence", never as a diagnosis.

## Renderers

Each takes a `Summary` and returns output; none reads the case.

- `to_markdown(summary) -> str`: short structured text for email, forums, and later the chat assistant.
- `to_html(summary) -> str`: a self-contained page (inline CSS, no external requests, theme-aware) with ruled-out list, verdict cards and before/after plots. User text is HTML-escaped.
- PDF is deferred; it can be rendered from the HTML later.

An optional narrative write-up from the chat assistant may later sit on top. The structured summary stays the source of truth.

## Code layout

```
crank/case.py       Case (session folder + events + profile), Event, Vehicle, append_event, load_events
crank/summary.py    build_summary, Summary
crank/render.py     to_markdown, to_html
crank/store.py      Session stays; Case wraps it
case_ui.py          the Case page (keeps app.py from growing)
```

## UI

**Case page** (landing page while a case has no captures):
- Vehicle panel: profile fields and problem statement, one save button.
- Quick add: Symptom, Observation, Change, Code, Measurement, Hypothesis, each a short form with an optional capture to attach to.
- Hypotheses board: open, ruled out, confirmed. Moving a card requires a reason. Seeded with suggestions from the latest verdict (a `ref_sag` finding suggests "5V reference circuit").
- Timeline: newest first, filterable by type.
- Report: preview that shows the "not recorded" gaps, Markdown download and copy, HTML download.

**Existing pages:**
- Capture and Guided tests append their events automatically (`capture` from the capture job's save step using the finished analysis; `measurement` per saved guided step).
- Compare gets "mark as before/after of a change", linking two captures to a `change` event.
- Results gets "Log verdict as observation".
- Phone remote gets a minimal quick add (Observation and Change only), no Case page.

## Testing

All without hardware.
- **Event log:** append and read back, ordering, unique ids; corrupt or truncated line skipped and counted; rapid appends do not interleave; hypothesis latest-wins; unknown types and extra fields load.
- **Legacy sessions:** a pre-existing session with no `events.jsonl` opens as a case and the summary includes synthesized events; no files rewritten.
- **Summary:** scripted cases checked for exact contents (ruled-out reasons and evidence, not-yet-tried steps, before/after only when a change separates two captures); empty case and profile-only case give a valid summary with "not recorded"; simulator captures labeled.
- **Renderers:** Markdown snapshots against fixed summaries; output never contains a value absent from the case; HTML has no external requests and escapes user text (a `<script>` in a note does not run).
- **UI:** `AppTest` for the Case page (add each event type, move a hypothesis, generate a preview) and the phone quick add.
- **Existing behavior:** the current 99 tests stay green.

## Error handling

- Events append with a single write; a crash loses at most the last line. `vehicle.json` uses the existing atomic write.
- Form input is validated before appending: a measurement needs a numeric value and a unit; a hypothesis status change needs a reason.
- A corrupt `vehicle.json` is set aside like the other JSON files; the case opens with an empty profile.
- A case with no events produces a report that says so rather than a plausible-looking empty hand-off.
- The report preview shows exactly what will be included before anything is copied or downloaded. The VIN is optional. The app uploads nothing.
