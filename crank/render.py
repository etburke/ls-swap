"""Render a Summary as Markdown or a self-contained HTML page. Renderers never read the case."""
from __future__ import annotations

from html import escape

import numpy as np

from .report import decimate
from .summary import NOT_RECORDED, Summary


def _v(x) -> str:
    return NOT_RECORDED if x in (None, "") else str(x)


def _md(x) -> str:
    """One line of Markdown-safe text: notes can be multi-line and `$` starts math in some viewers."""
    return " ".join(str(x).split()).replace("$", "\\$")


def _cap_head(c: dict) -> str:
    sim = " (SIMULATED)" if c["sim"] else ""
    return f"{c['id']} {c['label'] or 'unlabeled'}{sim}"


def _cap_tail(c: dict) -> str:
    snap = c["snapshot"]
    if not snap:
        return f"verdict {NOT_RECORDED}"
    top = snap["verdict"][0]["title"] if snap.get("verdict") else NOT_RECORDED
    bits = [f"{snap['revs_good']}/{snap['revs']} good revolutions" if snap.get("revs") else None,
            f"5V ref sag {snap['ref_sag_mv']} mV" if snap.get("ref_sag_mv") is not None
            else f"5V ref sag {NOT_RECORDED}",
            f"ground deviation {snap['gnd_dev_mv']} mV" if snap.get("gnd_dev_mv") is not None
            else f"ground deviation {NOT_RECORDED}"]
    return f"the harness reported \"{top}\" ({'; '.join(b for b in bits if b)})"


def _cap_line(c: dict) -> str:
    return f"{_cap_head(c)}: {_cap_tail(c)}"


def _code(c: dict) -> str:
    ecu = f", {c['ecu']}" if c.get("show_ecu") and c.get("ecu") else ""
    st_ = f" ({c['status']}{ecu})" if c.get("status") else ""
    sim = " [SIMULATED]" if c.get("sim") else ""
    return f"{c.get('code')}{st_}{sim} {c.get('description') or 'description not recorded'}"


def _measure(m: dict) -> str:
    sim = " [SIMULATED]" if m.get("sim") else ""
    return f"{m['name']}: {m['value']:g} {m['unit']}{sim}" + (f" ({m['where']})" if m["where"] else "")


def _freeze(ff: dict) -> str:
    sim = " [SIMULATED]" if ff.get("sim") else ""
    vals = "; ".join(f"{v['name']} {v['value']:g} {v['unit']}" for v in ff.get("values", []))
    return (f"Freeze frame{sim} for {ff.get('code')} {ff.get('description') or 'description not recorded'}: "
            + (vals or NOT_RECORDED))


INFERRED = "(inferred from the order of events)"


def to_markdown(s: Summary) -> str:
    v = s.vehicle
    out = [f"# Diagnostic summary: {v.title}", ""]
    if s.empty:
        return "\n".join(out + ["Nothing has been recorded for this case yet."]) + "\n"
    details = [("Engine", v.engine), ("Transmission", v.transmission), ("Mileage", v.mileage),
               ("Modifications", v.modifications)] + ([("VIN", v.vin)] if v.vin else [])
    out += [f"- {k}: {_md(_v(x))}" for k, x in details]
    out += ["", "## Problem", _md(v.problem) or NOT_RECORDED]
    if s.symptoms:
        out += ["", "## Symptoms"] + [f"- {_md(x['text'])}" for x in s.symptoms]
    out += ["", "## Ruled out"]
    out += ([f"- {_md(r['title'])}: {_md(r['reason'])}" + (f" (evidence: {', '.join(r['evidence'])})" if r["evidence"] else "")
             for r in s.ruled_out] or ["- nothing yet"])
    out += ["", "## Still open"] + ([f"- {_md(r['title'])}" for r in s.open] or ["- nothing listed"])
    if s.confirmed:
        out += ["", "## Confirmed"] + [f"- {_md(r['title'])}: {_md(r['reason'])}" for r in s.confirmed]
    out += ["", "## Key data"]
    if s.codes:
        out.append("- Codes: " + _md(", ".join(_code(c) for c in s.codes)))
    if s.freeze_frame:
        out.append("- " + _md(_freeze(s.freeze_frame)))
    out += [f"- {_md(_measure(m))}" for m in s.measurements]
    out += [f"- Capture {_md(_cap_line(c))}" for c in s.captures]
    if not (s.codes or s.measurements or s.captures):
        out.append(f"- {NOT_RECORDED}")
    if s.before_after:
        out += ["", "## Before and after"]
        for b in s.before_after:
            out += [f"- After \"{_md(b['change'])}\"{' ' + INFERRED if b['inferred'] else ''}:", f"  - before: {_md(_cap_line(b['before']))}", f"  - after: {_md(_cap_line(b['after']))}"]
    out += ["", "## Not yet tried"] + ([f"- {t}" for t in s.not_tried] or ["- all guided tests have data"])
    out += ["", "## Timeline"] + ([f"- {t['ts'][:16].replace('T', ' ') or 'undated'}: {_md(t['line'])}"
                                  for t in s.timeline] or ["- nothing recorded yet"])
    if s.attachments:
        out += ["", "## Attachments (in the case folder)"] + [f"- {a}" for a in s.attachments]
    if s.skipped_lines:
        out += ["", f"Note: {s.skipped_lines} damaged log line(s) were skipped."]
    out += ["", "_The verdicts are measurements and rule-based findings from a diagnostic tool, not a diagnosis._"]
    return "\n".join(out) + "\n"


def capture_svg(data, width: int = 560) -> str:
    """Two stacked traces (crank signal and 5V reference) as inline SVG; no external requests."""
    rows = [(1, "crank", 0.0, 6.0, "var(--crank)"), (3, "5V ref", 2.5, 5.5, "var(--ref)")]
    rows = [r for r in rows if r[0] in data.channels]
    if not rows:
        return ""
    m = (data.t >= data.t[0]) & (data.t <= data.t[0] + 0.8)
    ph, h = 70, 70 * len(rows) + 18 * len(rows)
    parts = [f'<svg viewBox="0 0 {width} {h}" role="img" aria-label="Capture traces">']
    for i, (ch, name, lo, hi, color) in enumerate(rows):
        y0 = 18 * (i + 1) + ph * i
        t, v = decimate(data.t[m], data.channels[ch][m], 700)
        x = (t - t[0]) / max(float(t[-1] - t[0]), 1e-9) * (width - 4) + 2
        y = y0 + ph - (np.clip(v, lo, hi) - lo) / (hi - lo) * ph
        pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(x, y))
        parts.append(f'<text x="2" y="{y0 - 5}" class="lab">{name} ({lo:g}-{hi:g} V, first 0.8 s)</text>'
                     f'<rect x="2" y="{y0}" width="{width - 4}" height="{ph}" class="box"/>'
                     f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.2"/>')
    return "".join(parts) + "</svg>"


CSS = """
:root{--bg:#f4f6f7;--fg:#16212b;--muted:#53636f;--rule:#c5cfd6;--panel:#fff;--crank:#1d62a8;--ref:#a5620a;
--bad:#b3261e;--sim:#9a5b00}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0f151a;--fg:#e4ebf0;--muted:#94a5b2;
--rule:#2b3a45;--panel:#172028;--crank:#6fb2f0;--ref:#e5a64a;--bad:#ff8a80;--sim:#f0b45c;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f151a;--fg:#e4ebf0;--muted:#94a5b2;--rule:#2b3a45;--panel:#172028;--crank:#6fb2f0;
--ref:#e5a64a;--bad:#ff8a80;--sim:#f0b45c;color-scheme:dark}
body{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:860px;margin:0 auto}h1{font-size:1.9rem;margin:0 0 4px}h2{font-size:1.15rem;border-top:2px solid var(--fg);
padding-top:8px;margin:28px 0 8px}ul{padding-left:1.2em}li{margin:3px 0}.muted{color:var(--muted)}
.sim{color:var(--sim);font-weight:600}.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--rule);padding:12px;min-width:0;overflow-x:auto}
svg{width:100%;height:auto}svg .lab{font:10px monospace;fill:var(--muted)}svg .box{fill:none;stroke:var(--rule)}
"""


def _li(items) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else ""


def _cap_html(c: dict, svg: str = "") -> str:
    sim = ' <span class="sim">SIMULATED</span>' if c["sim"] else ""
    return (f'<div class="card"><b>{escape(c["id"])} {escape(c["label"] or "unlabeled")}</b>{sim}'
            f'<br>{escape(_cap_tail(c))}{svg}</div>')


def to_html(s: Summary, plots: dict | None = None) -> str:
    """`plots` maps a capture id to an inline SVG string (from `capture_svg`)."""
    plots = plots or {}
    e, v = escape, s.vehicle
    body = [f"<h1>Diagnostic summary: {e(v.title)}</h1>"]
    if s.empty:
        body.append("<p>Nothing has been recorded for this case yet.</p>")
    else:
        body.append(_li(f"{k}: {e(_v(x))}" for k, x in [("Engine", v.engine), ("Transmission", v.transmission),
                                                          ("Mileage", v.mileage), ("Modifications", v.modifications)]))
        body.append(f"<h2>Problem</h2><p>{e(v.problem or NOT_RECORDED)}</p>")
        if s.symptoms:
            body.append("<h2>Symptoms</h2>" + _li(e(x["text"]) for x in s.symptoms))
        body.append("<h2>Ruled out</h2>" + (_li(f"<b>{e(r['title'])}</b>: {e(r['reason'])}"
                    + (f" <span class='muted'>(evidence: {e(', '.join(r['evidence']))})</span>" if r["evidence"] else "")
                    for r in s.ruled_out) or "<p class='muted'>nothing yet</p>"))
        body.append("<h2>Still open</h2>" + (_li(e(r["title"]) for r in s.open) or "<p class='muted'>nothing listed</p>"))
        if s.confirmed:
            body.append("<h2>Confirmed</h2>" + _li(f"{e(r['title'])}: {e(r['reason'])}" for r in s.confirmed))
        data = []
        if s.codes:
            data.append("Codes: " + e(", ".join(_code(c) for c in s.codes)))
        if s.freeze_frame:
            data.append(e(_freeze(s.freeze_frame)))
        data += [e(_measure(m)) for m in s.measurements]
        body.append("<h2>Key data</h2>" + (_li(data) or f"<p class='muted'>{NOT_RECORDED}</p>"))
        if s.captures:
            body.append('<div class="pair">' + "".join(_cap_html(c, plots.get(c["id"], "")) for c in s.captures) + "</div>")
        for b in s.before_after:
            body.append(f"<h2>Before and after: {e(b['change'])}"
                        f"{' <span class=muted>' + INFERRED + '</span>' if b['inferred'] else ''}</h2><div class='pair'>"
                        f"{_cap_html(b['before'], plots.get(b['before']['id'], ''))}"
                        f"{_cap_html(b['after'], plots.get(b['after']['id'], ''))}</div>")
        body.append("<h2>Not yet tried</h2>" + (_li(e(t) for t in s.not_tried) or "<p class='muted'>all guided tests have data</p>"))
        body.append("<h2>Timeline</h2>" + (_li([f"<span class='muted'>{e(t['ts'][:16].replace('T', ' ') or 'undated')}</span> "
                    f"{e(t['line'])}" for t in s.timeline]) or "<p class='muted'>nothing recorded yet</p>"))
        if s.skipped_lines:
            body.append(f"<p class='muted'>{s.skipped_lines} damaged log line(s) were skipped.</p>")
    body.append("<p class='muted'>The verdicts are measurements and rule-based findings from a diagnostic tool, not a diagnosis.</p>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>Diagnostic summary: {e(v.title)}</title>"
            f"<style>{CSS}</style></head><body><main>{''.join(body)}</main></body></html>")
