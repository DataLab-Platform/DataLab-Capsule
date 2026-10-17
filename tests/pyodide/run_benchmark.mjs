// Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.
//
// Run a benchmark of benchmarks/ in Pyodide hosted by Node.
// Usage:
//   node run_benchmark.mjs <wheel> [SCRIPT] [--profile P] [--repeat N]
//       [--output PATH] [--case ID]...
// SCRIPT is fingerprint_cost.py (default) or capsule_cost.py; the options are
// those of the benchmark (see benchmarks/_harness.py). Durations are measured
// in one Pyodide runtime; memory is measured case by case, each in a fresh
// Pyodide runtime, so that the WebAssembly heap growth of a case is not hidden
// by the growth of an earlier one. The report is written to --output on the
// host (printed otherwise). Results are those of Pyodide under Node, not of
// DataLab-Web in a browser.

import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { cpus, platform, release, totalmem } from "node:os";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide, version as pyodideVersion } from "pyodide";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..", "..");
const [wheel, ...rest] = process.argv.slice(2);
if (!wheel) {
  console.error("Usage: node run_benchmark.mjs <wheel> [SCRIPT] [options]");
  process.exit(2);
}
const script = rest.length && !rest[0].startsWith("--") ? rest.shift() : "fingerprint_cost.py";
const args = [...rest];
let output = null;
const outputIndex = args.indexOf("--output");
if (outputIndex >= 0) {
  output = resolve(args[outputIndex + 1]);
  args.splice(outputIndex, 2);
}
const passthrough = args.filter((_arg, index) => {
  // --case ID and --phase are set per run below
  return !(args[index] === "--phase" || args[index - 1] === "--phase");
});

let commit = null;
try {
  commit = execFileSync("git", ["-C", root, "rev-parse", "HEAD"]).toString().trim();
} catch {
  commit = null;
}
const environment = {
  DLC_BENCH_RUNTIME: "pyodide-node",
  DLC_BENCH_PYODIDE: pyodideVersion,
  DLC_BENCH_COMMIT: commit ?? "",
  DLC_BENCH_HOST: `${platform()} ${release()}, ${cpus()[0]?.model ?? "?"}, ${cpus().length} CPUs, ${Math.round(totalmem() / 1024 ** 3)} GiB, Node ${process.version}`,
};

async function runtime() {
  const py = await loadPyodide();
  await py.loadPackage(["numpy", "h5py", "micropip"]);
  const wheelName = basename(wheel);
  py.FS.writeFile(`/tmp/${wheelName}`, readFileSync(wheel));
  await py.runPythonAsync(`
import micropip
await micropip.install("emfs:/tmp/${wheelName}")
`);
  for (const name of ["_harness.py", script]) {
    py.FS.writeFile(`/tmp/${name}`, readFileSync(resolve(root, "benchmarks", name)));
  }
  return py;
}

async function run(py, argv) {
  py.globals.set("bench_argv", py.toPy(argv));
  py.globals.set("bench_env", py.toPy(environment));
  await py.runPythonAsync(`
import os, runpy, sys
os.environ.update(bench_env)
sys.argv = ["/tmp/${script}", *bench_argv]
try:
    runpy.run_path("/tmp/${script}", run_name="__main__")
except SystemExit as exc:
    if exc.code not in (None, 0):
        raise
`);
  return JSON.parse(py.FS.readFile("/tmp/report.json", { encoding: "utf8" }));
}

const timingRuntime = await runtime();
const report = await run(timingRuntime, [
  ...passthrough,
  "--phase",
  "timing",
  "--output",
  "/tmp/report.json",
]);
for (const entry of report.cases) {
  if (entry.status !== "ok" && entry.status !== "refused_by_policy") {
    entry.memory = { status: "not_measured", reason: `timing run: ${entry.status}` };
    continue;
  }
  const fresh = await runtime();
  const caseArgs = passthrough.filter(
    (_arg, index) => !(passthrough[index] === "--case" || passthrough[index - 1] === "--case"),
  );
  try {
    const memory = await run(fresh, [
      ...caseArgs,
      "--case",
      entry.id,
      "--repeat",
      "1",
      "--phase",
      "memory",
      "--output",
      "/tmp/report.json",
    ]);
    const measured = memory.cases[0];
    entry.memory = measured.memory ?? { status: measured.status, error: measured.error };
    if (measured.status !== entry.status) {
      entry.memory.status = measured.status;
    }
  } catch (error) {
    entry.memory = { status: "crash", error: String(error).slice(0, 500) };
  }
  console.error(
    `${entry.id}: memory ${entry.memory?.wasm_heap?.growth_bytes ?? "n/a"} bytes of heap growth`,
  );
}
report.phase = "all";
report.memory_methods.isolation = "one fresh Pyodide runtime per case for memory";
const text = JSON.stringify(report, null, 1);
if (output) {
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, text);
} else {
  console.log(text);
}
