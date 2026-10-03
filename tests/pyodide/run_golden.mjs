// Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.
//
// Run the DataLab-Capsule golden vectors in Pyodide hosted by Node.
// Usage: node run_golden.mjs <path/to/datalab_capsule-*.whl>
// Packages from the Pyodide lock (numpy, h5py, jsonschema...) are fetched from
// the Pyodide CDN on first use and cached next to the npm package.

import { readFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const here = dirname(fileURLToPath(import.meta.url));
const golden = resolve(here, "..", "golden");
const wheel = process.argv[2];
if (!wheel) {
  console.error("Usage: node run_golden.mjs <wheel>");
  process.exit(2);
}

const py = await loadPyodide();
await py.loadPackage(["numpy", "h5py", "micropip"]);
const wheelName = basename(wheel);
py.FS.writeFile(`/tmp/${wheelName}`, readFileSync(wheel));
await py.runPythonAsync(`
import micropip
await micropip.install("emfs:/tmp/${wheelName}")
`);

py.FS.mkdirTree("/golden/jcs");
for (const name of ["vectors.json", "check_golden.py"]) {
  py.FS.writeFile(`/golden/${name}`, readFileSync(join(golden, name)));
}
for (const name of ["rfc8785-sample.json", "rfc8785-sample.out"]) {
  py.FS.writeFile(`/golden/jcs/${name}`, readFileSync(join(golden, "jcs", name)));
}

const result = await py.runPythonAsync(`
import hashlib, io, json, sys, zipfile
import h5py, jsonschema, numpy
import datalab_capsule
sys.path.insert(0, "/golden")
import check_golden
failures = check_golden.run("/golden/vectors.json")
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
    archive.writestr("ro-crate-metadata.json", "{}")
with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
    if archive.read("ro-crate-metadata.json") != b"{}":
        failures.append("zip round trip")
if hashlib.sha256(b"abc").hexdigest() != (
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
):
    failures.append("hashlib sha256")
json.dumps({
    "failures": failures,
    "versions": {
        "python": sys.version.split()[0],
        "datalab_capsule": datalab_capsule.__version__,
        "numpy": numpy.__version__,
        "h5py": h5py.__version__,
    },
})
`);
const report = JSON.parse(result);
console.log(JSON.stringify(report.versions));
if (report.failures.length) {
  console.error("Golden vector failures:\n" + report.failures.join("\n"));
  process.exit(1);
}
console.log("Pyodide golden vectors: all passed");
