# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""Extract Matrix's in-app help tables from the SPA bundle source.

Key trick: the bundle's virtual-DOM render calls
    L(35,"tr")(36,"td"),Y(37," MAXSTOPS "),L(38,"span",14),Y(39,"n"),Q()()
are **syntactically valid Python**. Stub `L`/`Y`/`Q` as Python functions
that build a tree, eval the slice, walk the tree. No tokeniser, no
parser library.

Regenerate:
    uv run --script research/extract_help_docs.py
"""
from __future__ import annotations
import argparse, pathlib, re, sys
import httpx

UA = "Mozilla/5.0"

def fetch_bundle() -> str:
    c = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
    home = c.get("https://matrix.itasoftware.com/search").text
    url = re.search(r'src="((?:https:)?//www\.gstatic\.com/alkali/[^"]+\.js)"', home).group(1)
    return c.get(("https:" + url) if url.startswith("//") else url).text


def rows_in(bundle: str, start_anchor: str, end_anchor: str) -> list[list[str]]:
    """Slice the bundle to one help-table region; eval with L/Y/Q stubs;
    return rows as [[cell, …], …]."""
    s = bundle.find(start_anchor)
    if s < 0: raise SystemExit(f"start anchor not found: {start_anchor!r}")
    e = bundle.find(end_anchor, s)
    if e < 0: raise SystemExit(f"end anchor not found: {end_anchor!r}")
    # Walk back to the row's <tr> opener so the first cell is included.
    # `rfind('L(')` alone is wrong — it lands on the row's last `L(*,"td")`
    # opener instead. Find the closest `"tr"` token first, then back up to
    # its containing `L(`.
    tr = bundle.rfind('"tr"', max(0, s - 2000), s)
    s = bundle.rfind('L(', max(0, tr - 20), tr)
    # The end anchor lands inside a JS string literal; advance past the
    # closing quote first, then through the row's close-chain (`)`, `Q()`,
    # commas) until the next `L(` opener (= start of next section).
    pos = bundle.find('")', e + len(end_anchor))
    if pos < 0: raise SystemExit(f"unterminated end anchor: {end_anchor!r}")
    pos += 2  # past `")`
    m = re.match(r'[^L]*?(?=L\(|$)', bundle[pos:])
    e = pos + (m.end() if m else 0)

    root: dict = {"tag": "root", "children": []}
    stack: list[dict] = [root]
    def L(_n, tag, *_):
        node = {"tag": tag, "children": []}
        stack[-1]["children"].append(node)
        stack.append(node)
        return L
    def Y(_n, text):
        stack[-1]["children"].append(text)
    def Q():
        if len(stack) > 1:
            stack.pop()
        return Q
    sl = bundle[s:e].rstrip(", \t\n")
    exec(f"({sl},)", {"L": L, "Y": Y, "Q": Q})

    def text(n) -> str:
        return n if isinstance(n, str) else "".join(text(c) for c in n["children"])

    out: list[list[str]] = []
    def walk(n):
        if isinstance(n, str): return
        if n["tag"] == "tr":
            cells = [text(c).strip() for c in n["children"]
                     if isinstance(c, dict) and c["tag"] in ("td", "th")]
            if any(cells): out.append(cells)
        for c in n["children"]: walk(c)
    walk(root)
    return out


SECTIONS = [
    # (name, header, start-anchor inside first row, end-anchor inside last
    # row). The forward walk past end-anchor consumes close-chain syntax
    # until the next L( opener, so the anchor just needs to be unique text
    # somewhere inside the last row's cells.
    ("Itineraries",     ["Syntax", "Example", "Meaning"],
        '"-CODESHARE"',         'booked in another cabin'),
    ("Faring",          ["Syntax", "Example", "Meaning"],
        '"+CABIN 1"',           'start with either Y or B'),
    ("Aircraft Types",  ["Code", "Parent", "Aircraft"],
        '"AT4"',                'Yunshuji-5'),
]
SKIP_HEADERS = {"Syntax", "Code", "CODE", "Aircraft Name"}


def to_md(name: str, header: list[str], rows: list[list[str]]) -> str:
    body = [r for r in rows if r and r[0].strip() not in SKIP_HEADERS]
    lines = [f"## {name}\n", "| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for r in body:
        cells = (r + [""] * len(header))[:len(header)]
        lines.append("| " + " | ".join(c.replace("|", r"\|").strip() for c in cells) + " |")
    return "\n".join(lines) + "\n\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/memories/matrix_help_docs.md")
    args = ap.parse_args()
    bundle = fetch_bundle()
    print(f"→ bundle: {len(bundle):,} chars", file=sys.stderr)

    pieces = [
        "# Matrix in-app help docs (extracted from SPA bundle)\n\n",
        "Auto-extracted from gstatic.com/alkali/*.js by evaluating the\n"
        "bundle's `L`/`Y`/`Q` render calls as Python (they happen to be\n"
        "valid Python expressions). Regenerate:\n\n"
        "```\nuv run --script research/extract_help_docs.py\n```\n\n",
    ]
    for name, header, s, e in SECTIONS:
        rs = rows_in(bundle, s, e)
        print(f"   {name}: {len(rs)} rows", file=sys.stderr)
        pieces.append(to_md(name, header, rs))

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(pieces))
    print(f"→ {out} ({out.stat().st_size:,} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
