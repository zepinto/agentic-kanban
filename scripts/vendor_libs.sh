#!/usr/bin/env bash
# Fetch the chart and diagram libraries the board renders reports with into
# kanban_ui/static/vendor/, so it works offline. Without them the board loads
# the same files from cdn.jsdelivr.net on first use.
#
#   bash scripts/vendor_libs.sh            # default versions
#   MERMAID_VERSION=11.4.1 PLOTLY_VERSION=2.35.2 bash scripts/vendor_libs.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=kanban_ui/static/vendor
MERMAID_VERSION="${MERMAID_VERSION:-11.4.1}"
PLOTLY_VERSION="${PLOTLY_VERSION:-2.35.2}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() {  # npm package, version, path inside the tarball, output file
    local tgz="$TMP/$1.tgz"
    curl -fsSL "https://registry.npmjs.org/$1/-/$(basename "$1")-$2.tgz" -o "$tgz"
    tar -xzf "$tgz" -C "$TMP" "package/$3"
    cp "$TMP/package/$3" "$DEST/$4"
    rm -rf "$TMP/package"
    echo "$4  ← $1@$2 ($(du -h "$DEST/$4" | cut -f1))"
}

mkdir -p "$DEST"
fetch mermaid "$MERMAID_VERSION" dist/mermaid.min.js mermaid.min.js
fetch plotly.js-dist-min "$PLOTLY_VERSION" plotly.min.js plotly.min.js
