// Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.
//
// Run DataLab-Capsule checks in Pyodide hosted by Node:
// - the golden vectors (identical digests in CPython and Pyodide);
// - the pytest suite, on the same files as in CPython.
// Usage: node run_pyodide.mjs <path/to/datalab_capsule-*.whl>
// Packages from the Pyodide lock (numpy, h5py, jsonschema, pytest...) are fetched
// from the Pyodide CDN on first use and cached next to the npm package.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const here = dirname(fileURLToPath(import.meta.url));
const testsDir = resolve(here, "..");
const wheel = process.argv[2];
if (!wheel) {
  console.error("Usage: node run_pyodide.mjs <wheel>");
  process.exit(2);
}

function copyTree(py, source, target) {
  py.FS.mkdirTree(target);
  for (const name of readdirSync(source)) {
    if (name === "node_modules" || name === "__pycache__" || name === "pyodide") {
      continue;
    }
    const path = join(source, name);
    const dest = `${target}/${relative(source, path).replaceAll("\\", "/")}`;
    if (statSync(path).isDirectory()) {
      copyTree(py, path, dest);
    } else {
      py.FS.writeFile(dest, readFileSync(path));
    }
  }
}

const py = await loadPyodide();
await py.loadPackage(["numpy", "h5py", "micropip", "pytest"]);
const wheelName = basename(wheel);
py.FS.writeFile(`/tmp/${wheelName}`, readFileSync(wheel));
await py.runPythonAsync(`
import micropip
await micropip.install("emfs:/tmp/${wheelName}")
`);
copyTree(py, testsDir, "/work/tests");

const result = await py.runPythonAsync(`
import hashlib, io, json, os, sys, zipfile
import h5py, jsonschema, numpy, pytest
import datalab_capsule
os.chdir("/work")
sys.path[:0] = ["/work", "/work/tests/golden"]
import check_golden
failures = check_golden.run("/work/tests/golden/vectors.json")
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
code = int(pytest.main(["/work/tests", "-q", "-p", "no:cacheprovider",
                        "--rootdir", "/work", "--import-mode=importlib"]))
json.dumps({
    "failures": failures,
    "pytest": code,
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
}
if (report.pytest !== 0) {
  console.error(`pytest exit code in Pyodide: ${report.pytest}`);
}
if (report.failures.length || report.pytest !== 0) {
  process.exit(1);
}
console.log("Pyodide: golden vectors and pytest suite passed");
