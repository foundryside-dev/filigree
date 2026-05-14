from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_populate_presets_ignores_malformed_storage() -> None:
    script = """
global.localStorage = {
  store: { filigree_presets: "{" },
  getItem(key) { return this.store[key] ?? null; },
  setItem(key, value) { this.store[key] = String(value); },
};

const select = {
  innerHTML: "",
  children: [],
  appendChild(option) { this.children.push(option); },
};

global.document = {
  getElementById(id) { return id === "filterPreset" ? select : null; },
  createElement() { return { value: "", textContent: "" }; },
};

const { populatePresets } = await import("./src/filigree/static/js/filters.js");
populatePresets();

if (select.innerHTML !== '<option value="">Presets...</option>') {
  throw new Error(`unexpected options: ${select.innerHTML}`);
}
if (select.children.length !== 0) {
  throw new Error(`expected no preset options, got ${select.children.length}`);
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
