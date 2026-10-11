// Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.
//
// Run benchmarks/fingerprint_cost.py in Pyodide hosted by Node.
// Usage: node run_benchmark.mjs <path/to/datalab_capsule-*.whl>

import { readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const here = dirname(fileURLToPath(import.meta.url));
const wheel = process.argv[2];
if (!wheel) {
  console.error("Usage: node run_benchmark.mjs <wheel>");
  process.exit(2);
}
const py = await loadPyodide();
await py.loadPackage(["numpy", "micropip"]);
const wheelName = basename(wheel);
py.FS.writeFile(`/tmp/${wheelName}`, readFileSync(wheel));
await py.runPythonAsync(`
import micropip
await micropip.install("emfs:/tmp/${wheelName}")
`);
const script = resolve(here, "..", "..", "benchmarks", "fingerprint_cost.py");
py.FS.writeFile("/tmp/fingerprint_cost.py", readFileSync(script));
await py.runPythonAsync(`
import sys
sys.path.insert(0, "/tmp")
import fingerprint_cost
fingerprint_cost.main()
`);
