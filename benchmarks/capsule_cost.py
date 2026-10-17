# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Measure the time and memory cost of capsules and of their checks.

Run in CPython::

    python benchmarks/capsule_cost.py [--profile quick|representative|stress]
        [--repeat N] [--output PATH] [--case ID]...

Run in Pyodide (Node): ``node tests/pyodide/run_benchmark.mjs <wheel>
capsule_cost.py [same options]``.

Workspaces holding a provenance block are generated with a fixed seed (images
of a processing chain: a camera-like uint16 source, then float64 results), so
that the numbers do not depend on corpus files; nothing generated is written to
the repository. Each public operation is measured as exposed, with its own
total cost (an API that reads, validates and fingerprints is never split by
subtracting other measures):

- ``create``: :func:`archive.create_from_hdf5` with one attachment;
- ``read``: :func:`archive.read_capsule`;
- ``validate``: :func:`manifest.validate_manifest` of the manifest read;
- ``verify``: :func:`verify.verify_capsule` (stored states read with h5py);
- ``inspect``: :func:`exchange.inspect_capsule` with a supplied report and
  review; the number of archive reads it makes is recorded as a fact;
- ``open``: :func:`exchange.open_capsule`;
- ``attachment``: :func:`exchange.capsule_attachment`;
- ``registry-attachment``: :meth:`RegistryClient.get_checked_attachment` from
  a local registry (CPython with the ``registry`` extra only);
- ``inspect-referenced`` and ``open-referenced``: a capsule referencing its
  workspace, with the workspace file supplied.

Checks run once before the measures: the generated capsule is valid, every
stored state is located, and no computation library was imported (checking a
capsule never recomputes). Sizes: arrays, HDF5 file, archive and manifest,
objects, states and activities, and the compressibility of the workspace
(zlib on its first 8 MiB): HDF5 and array sizes are not assumed equal.

Profiles: ``quick`` (about 6.5 MiB of arrays), ``representative`` (about 34
and 136 MiB, and a ledger of 1000 activities on small signals), ``stress``
(about 234 MiB, close to the DataLab-Web limit of 256 MiB).
"""

from __future__ import annotations

import io
import json
import os.path as osp
import socket
import sys
import tempfile
import threading
import time
import zlib
from typing import Any

import h5py
import numpy as np

sys.path.insert(0, osp.dirname(osp.abspath(__file__)))

from _harness import Case, Unavailable, main  # noqa: E402

from datalab_capsule import exchange, verify  # noqa: E402
from datalab_capsule.archive import (  # noqa: E402
    MANIFEST_NAME,
    create_from_hdf5,
    read_capsule,
)
from datalab_capsule.calls import make_call  # noqa: E402
from datalab_capsule.compare import build_report, compare_environments  # noqa: E402
from datalab_capsule.environment import collect_environment  # noqa: E402
from datalab_capsule.hdf5 import save_ledger  # noqa: E402
from datalab_capsule.integrity import state_facts  # noqa: E402
from datalab_capsule.ledger import Ledger  # noqa: E402
from datalab_capsule.manifest import inspect_manifest, validate_manifest  # noqa: E402

QUICK, REPR, STRESS = "quick", "representative", "stress"
SEED = 20261017
REF = "https://data.example.org/benchmark/workspace.h5"
ATTACHMENT = {
    "name": "protocol.txt",
    "data": b"Benchmark protocol.\n" * 2000,
    "media_type": "text/plain",
    "description": "Protocol",
}
COMPUTATION_LIBRARIES = ("sigima", "scipy", "skimage", "cv2")

#: (workspace id, profiles, kind, count, size, dtype of the results); ids give
#: the size of the arrays: a uint16 source, then float64 results.
WORKSPACES = (
    ("ws-6mib", (QUICK, REPR, STRESS), "image", 4, 512, "float64"),
    ("ws-34mib", (REPR, STRESS), "image", 5, 1024, "float64"),
    ("ws-136mib", (REPR, STRESS), "image", 5, 2048, "float64"),
    ("ws-ledger-1000", (REPR, STRESS), "signal", 1001, 100, "float64"),
    ("ws-234mib", (STRESS,), "image", 30, 1024, "float64"),
)


def _uuid(index: int) -> str:
    return f"00000000-0000-4000-8000-{index:012d}"


def _image(data: np.ndarray) -> Any:
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel

    return SimpleNamespace(
        data=data,
        is_uniform_coords=True,
        x0=0.0,
        y0=0.0,
        dx=1.0,
        dy=1.0,
        xcoords=None,
        ycoords=None,
        xunit="mm",
        yunit="mm",
        zunit="counts",
        roi=None,
    )


def _signal(x: np.ndarray, y: np.ndarray) -> Any:
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel

    return SimpleNamespace(x=x, y=y, dx=None, dy=None, xunit="s", yunit="V", roi=None)


def _objects(kind: str, count: int, size: int, dtype: str) -> list[Any]:
    """Return the objects of a chain: a camera-like source, then results."""
    rng = np.random.default_rng(SEED)
    objects = []
    if kind == "image":
        yy, xx = np.mgrid[0:size, 0:size] / size
        base = 2000 + 1500 * np.sin(6 * xx) * np.cos(4 * yy)
        source = (base + rng.normal(0, 40, (size, size))).astype("uint16")
        objects.append(_image(source))
        for index in range(1, count):
            noise = rng.normal(0, 1e-3 * index, (size, size))
            objects.append(_image((source / 4096.0 + noise).astype(dtype)))
    else:
        x = np.linspace(0.0, 1.0, size)
        for index in range(count):
            y = np.sin(2 * np.pi * (index + 1) * x) + rng.normal(0, 0.01, size)
            objects.append(_signal(x, y.astype(dtype)))
    return objects


def _write(h5file: h5py.File, kind: str, index: int, obj: Any) -> None:
    if kind == "image":
        group = h5file.create_group(f"/DataLab_Ima/g001: Group/i{index:04d}: I{index}")
        group.attrs.update(
            {"x0": 0.0, "y0": 0.0, "dx": 1.0, "dy": 1.0, "is_uniform_coords": True}
        )
        group.attrs.update({"xunit": "mm", "yunit": "mm", "zunit": "counts"})
        group["data"] = obj.data
        group["xcoords"], group["ycoords"] = np.array([]), np.array([])
    else:
        group = h5file.create_group(f"/DataLab_Sig/g001: Group/s{index:04d}: S{index}")
        group.attrs["xunit"], group.attrs["yunit"] = "s", "V"
        group["xydata"] = np.vstack([obj.x, obj.y])
    group.create_group("metadata").attrs["__uuid"] = _uuid(index)


def generate(kind: str, count: int, size: int, dtype: str) -> dict[str, Any]:
    """Return a workspace (HDF5 bytes) holding a chain and its provenance."""
    objects = _objects(kind, count, size, dtype)
    environment = collect_environment("desktop", "benchmark", packages=("numpy",))
    operation = (
        "sigima.image.gaussian_filter"
        if kind == "image"
        else ("sigima.signal.normalize")
    )
    ledger = Ledger()
    previous = ledger.observe(_uuid(0), state_facts(objects[0]))
    for index, obj in enumerate(objects[1:], start=1):
        activity = ledger.record_activity(
            call=make_call(operation, 1, {"sigma": 1.0}, [("source", previous)]),
            outputs=[("result", _uuid(index), state_facts(obj))],
            environment=environment,
            edition="desktop",
            origin="ordinary",
        )
        state = ledger.observe(_uuid(index), state_facts(obj))
        assert state == activity["outputs"][0]["state_id"]
        previous = state
    buffer = io.BytesIO()
    with h5py.File(buffer, "w") as h5file:
        for index, obj in enumerate(objects):
            _write(h5file, kind, index, obj)
        h5file["DataLab_Version"] = "benchmark"
        save_ledger(h5file, ledger)
    workspace = buffer.getvalue()
    arrays = sum(
        int(o.data.nbytes) if kind == "image" else int(o.x.nbytes + o.y.nbytes)
        for o in objects
    )
    sample = workspace[: 8 * 1024**2]
    return {
        "workspace": workspace,
        "array_bytes": arrays,
        "objects": len(objects),
        "states": len(ledger.states),
        "activities": len(ledger.activities),
        "compressibility": round(len(sample) / len(zlib.compress(sample, 6)), 2),
    }


def _documents(manifest: dict[str, Any]) -> tuple[str, str]:
    activity_id = inspect_manifest(manifest)["activities"][0]["id"].lstrip("#")
    report = build_report(
        activity_id=activity_id,
        restoration="replayable",
        eligibility="ready",
        inputs=[],
        reference=None,
        environment=compare_environments(None, None, None, None),
    )
    review = exchange.review_of(
        manifest, reviewer="Benchmark", statement="Checked.", verdict="comment"
    )
    return json.dumps(report), json.dumps(review)


def setup_workspace(kind: str, count: int, size: int, dtype: str) -> dict[str, Any]:
    """Prepare a workspace, its capsules and documents (never timed)."""
    context = generate(kind, count, size, dtype)
    workspace = context["workspace"]
    context["capsule"] = create_from_hdf5(
        workspace, name="benchmark", attachments=[ATTACHMENT]
    )
    context["referenced"] = create_from_hdf5(
        workspace, name="benchmark", workspace_ref=REF
    )
    capsule = read_capsule(context["capsule"])
    context["manifest"] = capsule.manifest
    with zipfile_open(context["capsule"]) as archive:
        context["manifest_bytes"] = archive.getinfo(MANIFEST_NAME).file_size
    context["report"], context["review"] = _documents(capsule.manifest)
    context["reads"] = None
    return context


def zipfile_open(data: bytes) -> Any:
    """Open capsule bytes as a ZIP archive."""
    import zipfile  # pylint: disable=import-outside-toplevel

    return zipfile.ZipFile(io.BytesIO(data))


def _sizes(context: dict[str, Any]) -> dict[str, Any]:
    sizes = {
        "array_bytes": context["array_bytes"],
        "hdf5_bytes": len(context["workspace"]),
        "archive_bytes": len(context["capsule"]),
        "referenced_archive_bytes": len(context["referenced"]),
        "manifest_bytes": context["manifest_bytes"],
        "attachment_bytes": len(ATTACHMENT["data"]),
        "objects": context["objects"],
        "states": context["states"],
        "activities": context["activities"],
        "workspace_compressibility": context["compressibility"],
    }
    if context.get("reads") is not None:
        sizes["inspect_archive_reads"] = context["reads"]
    return sizes


def _no_computation() -> None:
    loaded = [name for name in COMPUTATION_LIBRARIES if name in sys.modules]
    assert not loaded, f"computation libraries imported: {loaded}"


def _check_verified(context: dict[str, Any], result: dict[str, Any]) -> None:
    assert result["valid"], result.get("problems")
    assert result["workspace"] == "verified", result["workspace"]
    assert result["states_located"] == context["objects"], result["states_located"]
    _no_computation()


def _inspect(context: dict[str, Any]) -> dict[str, Any]:
    return exchange.inspect_capsule(
        context["capsule"], reports=[context["report"]], reviews=[context["review"]]
    )


def _check_inspected(context: dict[str, Any], result: dict[str, Any]) -> None:
    _check_verified(context, result["integrity"])
    documents = result["documents"]
    assert [d["status"] for d in documents["reviews"]] == ["valid"], documents
    # Count the archive reads of one inspection, as a fact of the case
    calls = []
    originals = (exchange.read_capsule, verify.read_capsule)

    def counted(*args, **kwargs):
        calls.append(1)
        return originals[0](*args, **kwargs)

    exchange.read_capsule = verify.read_capsule = counted
    try:
        _inspect(context)
    finally:
        exchange.read_capsule, verify.read_capsule = originals
    context["reads"] = len(calls)


def _check_open(context: dict[str, Any], result: Any) -> None:
    workspace, origin = result
    assert workspace == context["workspace"]
    assert origin["fingerprint"] == inspect_manifest(context["manifest"])["fingerprint"]


class LocalRegistry:
    """A registry served by uvicorn in a thread, with one published capsule."""

    def __init__(self, capsule: bytes) -> None:
        try:
            import uvicorn  # pylint: disable=import-outside-toplevel

            from datalab_capsule.registry import Registry  # noqa: PLC0415
            from datalab_capsule.registry_app import create_app  # noqa: PLC0415
            from datalab_capsule.registry_client import RegistryClient  # noqa: PLC0415
        except ImportError as exc:
            raise Unavailable(f"registry extra not installed ({exc})") from exc
        if sys.platform == "emscripten":
            raise Unavailable("no HTTP server in Pyodide")
        self.folder = tempfile.TemporaryDirectory()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = create_app(Registry(osp.join(self.folder.name, "registry")), token="t")
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _index in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        self.client = RegistryClient(f"http://127.0.0.1:{port}", "t")
        self.fingerprint = self.client.publish_capsule(capsule)["fingerprint"]
        summary = inspect_manifest(read_capsule(capsule).manifest)
        (self.attachment,) = summary["attachments"]

    def download(self) -> bytes:
        """Download the attachment, checked against the manifest."""
        attachment = self.attachment
        return self.client.get_checked_attachment(
            self.fingerprint,
            attachment["name"],
            attachment["sha256"],
            attachment["size"],
        )

    def __del__(self) -> None:
        server = getattr(self, "server", None)
        if server is not None:
            server.should_exit = True
            self.thread.join(5)
            self.folder.cleanup()


def _registry_setup(make: Any) -> Any:
    def setup() -> dict[str, Any]:
        context = make()
        context["registry"] = LocalRegistry(context["capsule"])
        return context

    return setup


def cases(_profile: str) -> list[Case]:
    """Return every case; the harness keeps those of the profile."""
    result: list[Case] = []
    for ws_id, profiles, kind, count, size, dtype in WORKSPACES:
        params = {"workspace": ws_id, "kind": kind, "objects": count}
        params.update(size=size, results_dtype=dtype, seed=SEED)

        def make(k=kind, c=count, s=size, d=dtype) -> dict[str, Any]:
            return setup_workspace(k, c, s, d)

        operations = (
            (
                "create",
                lambda ctx: create_from_hdf5(
                    ctx["workspace"], name="benchmark", attachments=[ATTACHMENT]
                ),
                None,
            ),
            ("read", lambda ctx: read_capsule(ctx["capsule"]), None),
            ("validate", lambda ctx: validate_manifest(ctx["manifest"]), None),
            (
                "verify",
                lambda ctx: verify.verify_capsule(ctx["capsule"]),
                _check_verified,
            ),
            ("inspect", _inspect, _check_inspected),
            ("open", lambda ctx: exchange.open_capsule(ctx["capsule"]), _check_open),
            (
                "attachment",
                lambda ctx: exchange.capsule_attachment(ctx["capsule"], "protocol.txt"),
                None,
            ),
            (
                "inspect-referenced",
                lambda ctx: exchange.inspect_capsule(
                    ctx["referenced"], workspace=ctx["workspace"]
                ),
                lambda ctx, res: _check_verified(ctx, res["integrity"]),
            ),
            (
                "open-referenced",
                lambda ctx: exchange.open_capsule(ctx["referenced"], ctx["workspace"]),
                None,
            ),
        )
        for name, run, check in operations:
            result.append(
                Case(
                    id=f"{ws_id}.{name}",
                    profiles=profiles,
                    params={**params, "operation": name},
                    setup=make,
                    run=run,
                    sizes=_sizes,
                    check=check,
                    group=ws_id,
                )
            )
        if ws_id == "ws-6mib":
            result.append(
                Case(
                    id=f"{ws_id}.registry-attachment",
                    profiles=profiles,
                    params={**params, "operation": "registry-attachment"},
                    setup=_registry_setup(make),
                    run=lambda ctx: ctx["registry"].download(),
                    sizes=_sizes,
                    check=_check_attachment,
                )
            )
    return result


def _check_attachment(_context: dict[str, Any], data: bytes) -> None:
    assert data == ATTACHMENT["data"], "the registry returned other bytes"


if __name__ == "__main__":
    main(__file__, cases, __doc__.splitlines()[0])
