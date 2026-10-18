"""Build the static demo site.

Injects experiments/results/*.json into site/app.html and writes
docs/index.html (a complete document for GitHub Pages).

    python scripts/build_site.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "experiments" / "results"


def load(name):
    p = RES / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def main() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from pol2_agent_ballot.theory import cascade, condorcet, consensus

    data = {"headline": load("headline"), "sweep": load("sweep"), "consensus": load("consensus")}
    for r in data["headline"] or []:
        r["exact_accuracy"] = condorcet(r["n"], r["p"]) if r["condition"] == "sealed" else cascade(r["n"], r["p"], r["lead"])["accuracy"]
    for r in data["consensus"] or []:
        r["exact_reached"] = consensus(r["n"], r["p"], r["stubborn"])["reached"]
    body = (ROOT / "site" / "app.html").read_text().replace("/*__RESULTS__*/null", json.dumps(data, separators=(",", ":")))
    head_end = body.index("<div class=\"wrap\">")
    page = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        + body[:head_end]
        + "</head>\n<body>\n"
        + body[head_end:]
        + "\n</body>\n</html>\n"
    )
    out = ROOT / "docs" / "index.html"
    out.write_text(page)
    (ROOT / "docs" / ".nojekyll").write_text("")
    print("wrote", out)


if __name__ == "__main__":
    main()
