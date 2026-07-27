#!/usr/bin/env bash
# Build the arc42 documentation as HTML.
# Requires: pandoc. Optional: plantuml (for diagram images).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Optional: render PlantUML diagrams to PNG
if command -v plantuml >/dev/null 2>&1; then
    echo "→ Rendering PlantUML diagrams to PNG..."
    plantuml -tpng diagrams/*.puml -o png
elif command -v java >/dev/null 2>&1 && [[ -f /usr/share/plantuml/plantuml.jar ]]; then
    echo "→ Rendering PlantUML diagrams via jar..."
    java -jar /usr/share/plantuml/plantuml.jar -tpng diagrams/*.puml -o png
else
    echo "⚠ plantuml not available — diagrams will be referenced as .puml source"
fi

# Convert arc42 markdown to HTML
echo "→ Building arc42.html via pandoc..."
pandoc index.md \
    -o arc42.html \
    --standalone \
    --toc \
    --toc-depth=2 \
    --metadata title="radio-ripper-tag — arc42-Dokumentation" \
    --metadata lang=de

echo "✓ Built $(pwd)/arc42.html"