"""Build a self-contained interactive HTML report from experiments/results/*.json.

Run after sealed_vs_open.py:  python experiments/make_report.py
Writes experiments/figures/report.html (no external dependencies).
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
OUT = HERE / "figures" / "report.html"

SEALED = "#2E6FD6"
OPEN = "#D9822B"


def line_chart(rows, key, xlabel, title, width=460, height=300):
    pad_l, pad_r, pad_t, pad_b = 48, 70, 36, 44
    pw, ph = width - pad_l - pad_r, height - pad_t - pad_b
    xs_all = sorted({r[key] for r in rows})
    x0, x1 = xs_all[0], xs_all[-1]
    y0, y1 = 0.4, 1.0

    def X(v):
        return pad_l + (v - x0) / (x1 - x0) * pw

    def Y(v):
        return pad_t + (y1 - v) / (y1 - y0) * ph

    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" font-family="system-ui, sans-serif">']
    parts.append(f'<text x="{pad_l}" y="20" font-size="13" fill="#1F2937">{title}</text>')
    for yv in (0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        parts.append(f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{Y(yv):.1f}" y2="{Y(yv):.1f}" stroke="#E5E7EB"/>')
        parts.append(f'<text x="{pad_l-6}" y="{Y(yv)+4:.1f}" font-size="10" fill="#6B7280" text-anchor="end">{yv:.1f}</text>')
    for xv in xs_all:
        parts.append(f'<text x="{X(xv):.1f}" y="{height-pad_b+16}" font-size="10" fill="#6B7280" text-anchor="middle">{xv}</text>')
    parts.append(f'<text x="{pad_l+pw/2:.1f}" y="{height-6}" font-size="11" fill="#6B7280" text-anchor="middle">{xlabel}</text>')
    if key == "p":
        parts.append(f'<line x1="{X(x0):.1f}" y1="{Y(x0):.1f}" x2="{X(x1):.1f}" y2="{Y(x1):.1f}" stroke="#D1D5DB" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{X(x1)+6:.1f}" y="{Y(x1)+4:.1f}" font-size="9" fill="#9CA3AF">single agent</text>')
    for cond, color in (("sealed", SEALED), ("open", OPEN)):
        rs = sorted([r for r in rows if r["condition"] == cond], key=lambda r: r[key])
        band = " ".join(f"{X(r[key]):.1f},{Y(r['ci95'][1]):.1f}" for r in rs) + " " + " ".join(f"{X(r[key]):.1f},{Y(r['ci95'][0]):.1f}" for r in reversed(rs))
        parts.append(f'<polygon points="{band}" fill="{color}" opacity="0.15"/>')
        pts = " ".join(f"{X(r[key]):.1f},{Y(r['accuracy']):.1f}" for r in rs)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>')
        for r in rs:
            parts.append(
                f'<circle cx="{X(r[key]):.1f}" cy="{Y(r["accuracy"]):.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="2">'
                f'<title>{cond}: {key}={r[key]}  P(correct)={r["accuracy"]:.3f}  CI [{r["ci95"][0]:.3f}, {r["ci95"][1]:.3f}]  conformity={r["conformity_rate"]:.3f}</title></circle>'
            )
        last = rs[-1]
        parts.append(f'<text x="{X(last[key])+8:.1f}" y="{Y(last["accuracy"])+4:.1f}" font-size="11" fill="{color}">{cond}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def bar_chart(headline, field, title, ymax, fmt, width=320, height=260):
    pad_l, pad_t, pad_b = 48, 36, 36
    pw, ph = width - pad_l - 20, height - pad_t - pad_b
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" font-family="system-ui, sans-serif">']
    parts.append(f'<text x="{pad_l}" y="20" font-size="13" fill="#1F2937">{title}</text>')
    base = pad_t + ph
    bw = pw / len(headline) * 0.5
    for i, r in enumerate(headline):
        cx = pad_l + pw / len(headline) * (i + 0.5)
        v = r[field]
        h = v / ymax * ph
        color = SEALED if r["condition"] == "sealed" else OPEN
        parts.append(f'<rect x="{cx-bw/2:.1f}" y="{base-h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="4" fill="{color}"><title>{r["condition"]}: {fmt % v}</title></rect>')
        if field == "accuracy":
            lo, hi = r["ci95"]
            parts.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{base-lo/ymax*ph:.1f}" y2="{base-hi/ymax*ph:.1f}" stroke="#1F2937"/>')
        parts.append(f'<text x="{cx:.1f}" y="{base-h-6:.1f}" font-size="11" fill="#1F2937" text-anchor="middle">{fmt % v}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{base+16}" font-size="11" fill="#6B7280" text-anchor="middle">{r["condition"]}</text>')
    parts.append(f'<line x1="{pad_l}" x2="{width-20}" y1="{base}" y2="{base}" stroke="#E5E7EB"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    headline = json.loads((RESULTS / "headline.json").read_text())
    sweep = json.loads((RESULTS / "sweep.json").read_text()) if (RESULTS / "sweep.json").exists() else None
    h0 = headline[0]
    rows = "".join(
        f"<tr><td>{r['condition']}</td><td>{r['n']}</td><td>{r['p']}</td><td>{r['trials']:,}</td><td>{r['accuracy']:.4f}</td><td>{r['ci95'][0]:.4f}–{r['ci95'][1]:.4f}</td><td>{r['conformity_rate']:.4f}</td><td>{'protocol' if r['protocol'] else 'fast'}</td><td>{r['seconds']}</td></tr>"
        for r in headline
    )
    diff = headline[0]["accuracy"] - headline[1]["accuracy"]
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sealed vs open</title>
<style>
:root{{--ink:#1F2937;--muted:#6B7280;--bg:#fff;--card:#F9FAFB;--line:#E5E7EB}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--ink:#E5E7EB;--muted:#9CA3AF;--bg:#111827;--card:#1F2937;--line:#374151}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin-inline:auto}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--muted);margin:0 0 20px}}
.grid{{display:flex;flex-wrap:wrap;gap:16px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}}
.hero{{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:16px}} .hero div{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 18px}}
.hero b{{font-size:28px;display:block}} .hero span{{color:var(--muted);font-size:13px}}
table{{border-collapse:collapse;font-size:13px;width:100%}} td,th{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left}}
svg text{{fill:var(--ink)}} .note{{color:var(--muted);font-size:13px;margin-top:18px}}
</style></head><body>
<h1>Sealed vs open ballots — information cascades among agents</h1>
<p class="sub">pol2-agent-ballot · experiment 1 · N={h0['n']} agents, private accuracy p={h0['p']}, conformity lead={h0['lead']}, {h0['trials']:,} trials per condition through the full ballot protocol (every trial audited)</p>
<div class="hero">
<div><b style="color:{SEALED}">{headline[0]['accuracy']:.3f}</b><span>P(correct) · sealed</span></div>
<div><b style="color:{OPEN}">{headline[1]['accuracy']:.3f}</b><span>P(correct) · open</span></div>
<div><b>{diff:+.3f}</b><span>sealed − open</span></div>
<div><b>{headline[1]['conformity_rate']:.1%}</b><span>agents overriding their own signal (open)</span></div>
</div>
<div class="grid">
<div class="card">{bar_chart(headline, "accuracy", "P(correct collective decision)", 1.0, "%.3f")}</div>
<div class="card">{bar_chart(headline, "conformity_rate", "Share voting against own signal", max(0.05, headline[1]['conformity_rate']*1.4), "%.3f")}</div>
</div>
{"<h2 style='font-size:17px;margin:24px 0 8px'>Parameter sweeps (decision logic only, " + f"{sweep['by_n'][0]['trials']:,}" + " trials per point)</h2><div class='grid'><div class='card'>" + line_chart(sweep['by_n'], 'n', 'number of agents N', f"Accuracy vs N (p={sweep['by_n'][0]['p']})") + "</div><div class='card'>" + line_chart(sweep['by_p'], 'p', 'private signal accuracy p', f"Accuracy vs p (N={sweep['by_p'][0]['n']})") + "</div></div>" if sweep else ""}
<h2 style="font-size:17px;margin:24px 0 8px">Headline numbers</h2>
<table><tr><th>condition</th><th>N</th><th>p</th><th>trials</th><th>P(correct)</th><th>Wilson 95% CI</th><th>conformity</th><th>path</th><th>s</th></tr>{rows}</table>
<p class="note"><b>Honesty clause.</b> Scripted conformist agents with a hand-set rule (follow a visible majority once it leads by {h0['lead']}). The experiment shows the mechanism the sealed ballot protects against; it is not evidence about how LLM agents or humans behave. Hover a mark for its numbers. Shaded bands and error bars are Wilson 95% intervals.</p>
</body></html>"""
    OUT.write_text(html)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
