#!/usr/bin/env bash
#
# Render docs/TECHNICAL_BRIEF.md to docs/TECHNICAL_BRIEF.pdf.
#
# The Markdown is the source of truth. This exists so the PDF is reproducible
# rather than a one-off export nobody can regenerate — an earlier revision was
# rendered with headless Chrome and there was no record of the settings, which
# is how a deliverable drifts out of step with its source.
#
# The brief must be 2–3 pages (a stated requirement). The type sizes below are
# tuned to that, so changing them without checking the page count defeats the
# point. The script prints the count and fails if it lands outside the range.
#
#   ./scripts/build_brief.sh
#
# Requires: pandoc, wkhtmltopdf, qpdf, python3.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Which brief to render. Defaults to the architecture brief; pass a name to
# render the round-two response instead:
#
#   ./scripts/build_brief.sh                # TECHNICAL_BRIEF
#   ./scripts/build_brief.sh BRIEF_ROUND2   # the clinic-scenario response
#
# Both must land in 2-3 pages, which is why the page check below is not
# parameterised alongside the filename.
BRIEF="${1:-TECHNICAL_BRIEF}"
SRC="$REPO_ROOT/docs/$BRIEF.md"
OUT="$REPO_ROOT/docs/$BRIEF.pdf"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for tool in pandoc wkhtmltopdf qpdf python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

cat > "$WORK/brief.css" <<'CSS'
@page { size: A4; margin: 14mm 15mm; }
body {
  font-family: "Liberation Sans", Arial, Helvetica, sans-serif;
  font-size: 8.3pt; line-height: 1.30; color: #111; margin: 0;
}
h1 { font-size: 15pt; margin: 0 0 5pt; }
h2 { font-size: 10.5pt; margin: 10pt 0 3pt; border-bottom: 0.6pt solid #999; padding-bottom: 1.5pt; }
p  { margin: 0 0 4pt; text-align: justify; }
code, pre { font-family: "DejaVu Sans Mono", monospace; font-size: 7.2pt; }
pre { background: #f5f5f5; padding: 4pt 5pt; margin: 4pt 0; line-height: 1.22;
      border: 0.4pt solid #ddd; white-space: pre; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; font-size: 7.6pt; }
th, td { border: 0.4pt solid #bbb; padding: 1.6pt 3pt; text-align: left; vertical-align: top; }
th { background: #eee; }
strong { font-weight: bold; }
CSS

# Fragment, then wrap by hand. `pandoc -s` would emit its own <h1> from the
# metadata title on top of the one already in the Markdown, giving the rendered
# brief two titles.
pandoc "$SRC" -f gfm -t html5 -o "$WORK/body.html"

python3 - "$WORK" <<'PY'
import pathlib, sys
work = pathlib.Path(sys.argv[1])
body = (work / "body.html").read_text(encoding="utf-8")
css = (work / "brief.css").read_text(encoding="utf-8")
(work / "brief.html").write_text(
    '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
    '<title>Care Note - Technical Brief</title>\n'
    f"<style>{css}</style>\n</head><body>\n{body}\n</body></html>",
    encoding="utf-8",
)
PY

wkhtmltopdf --quiet --enable-local-file-access --encoding utf-8 --page-size A4 \
  --margin-top 14mm --margin-bottom 14mm --margin-left 15mm --margin-right 15mm \
  "$WORK/brief.html" "$WORK/raw.pdf" 2>/dev/null

# wkhtmltopdf emits a trailing blank page when the content ends near a boundary.
# Drop any page with no extractable text rather than assuming a fixed count.
python3 - "$WORK" <<'PY'
import pathlib, re, subprocess, sys
work = pathlib.Path(sys.argv[1])
raw = work / "raw.pdf"
total = len(re.findall(rb"/Type\s*/Page[^s]", raw.read_bytes()))
keep = []
for page in range(1, total + 1):
    text = subprocess.run(
        ["pdftotext", "-f", str(page), "-l", str(page), str(raw), "-"],
        capture_output=True, text=True,
    ).stdout.strip()
    if text:
        keep.append(str(page))
(work / "keep.txt").write_text(",".join(keep))
PY

qpdf "$WORK/raw.pdf" --pages . "$(cat "$WORK/keep.txt")" -- "$WORK/final.pdf"

PAGES=$(python3 -c "
import re,sys
print(len(re.findall(rb'/Type\s*/Page[^s]', open(sys.argv[1],'rb').read())))
" "$WORK/final.pdf")

if [ "$PAGES" -lt 2 ] || [ "$PAGES" -gt 3 ]; then
  echo "brief rendered to $PAGES pages; the requirement is 2-3. Not installing." >&2
  exit 1
fi

cp "$WORK/final.pdf" "$OUT"
echo "docs/$BRIEF.pdf rebuilt — $PAGES pages."
