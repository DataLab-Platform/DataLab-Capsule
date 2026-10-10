# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for the HDF5 provenance block and the expressiveness fixtures."""

from __future__ import annotations

import io
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from datalab_capsule import hdf5 as hdf5_module
from datalab_capsule.compare import validate_report
from datalab_capsule.hdf5 import (
    BLOCK_GROUP,
    ProvenanceFormatError,
    build_locators,
    load_ledger,
    locate_current_states,
    locate_fingerprints,
    read_block,
    read_image,
    read_signal,
    save_ledger,
    scan_object_index,
    write_block,
)
from datalab_capsule.integrity import signal_state_facts, state_facts
from datalab_capsule.ledger import Ledger

from .helpers import image, signal

FIXTURES = Path(__file__).parent / "fixtures" / "expressiveness"
LEDGER_FIXTURES = sorted(
    p for p in FIXTURES.glob("*.json") if "ledger" in json.loads(p.read_text("utf-8"))
)
OBJ_UUID = "00000000-0000-4000-8000-000000000001"


def _memory_file() -> h5py.File:
    return h5py.File(io.BytesIO(), "w")


def _write_signal(h5file, path: str, sig, object_uuid: str) -> None:
    group = h5file.create_group(path)
    group.attrs["xunit"] = sig.xunit
    group.attrs["yunit"] = sig.yunit
    group["xydata"] = np.vstack([sig.x, sig.y])
    group.create_group("metadata").attrs["__uuid"] = object_uuid


def _write_image(h5file, path, data, object_uuid, coords=None, x0=0.0, dx=1.0):
    """Write an image object with the layout of both editions."""
    group = h5file.create_group(path)
    group.attrs.update(
        {"x0": x0, "y0": 0.0, "dx": dx, "dy": 1.0, "is_uniform_coords": coords is None}
    )
    group.attrs.update({"xunit": "mm", "yunit": "mm", "zunit": "counts"})
    group["data"] = data
    xcoords, ycoords = (np.array([]), np.array([])) if coords is None else coords
    group["xcoords"], group["ycoords"] = xcoords, ycoords
    group.create_group("metadata").attrs["__uuid"] = object_uuid


def test_fixtures_are_up_to_date() -> None:
    """The committed JSON files match their generator."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("generate", FIXTURES / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, fixture in module.build().items():
        text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        assert text.replace("\r\n", "\n") == module.render(fixture)


@pytest.mark.parametrize("path", LEDGER_FIXTURES, ids=lambda p: p.stem)
def test_fixture_round_trips(path: Path) -> None:
    """Each fixture validates and round-trips through JSON and the HDF5 block."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    ledger = Ledger.from_dict(fixture["ledger"])
    assert json.loads(ledger.to_json()) == fixture["ledger"]
    with _memory_file() as h5file:
        write_block(h5file, ledger, fixture["locators"])
        restored, locators = read_block(h5file)
    assert restored.to_dict() == fixture["ledger"]
    assert locators == fixture["locators"]
    for original, copy_ in zip(fixture["ledger"]["activities"], restored.activities):
        assert [i["binding"] for i in copy_["call"]["inputs"]] == [
            i["binding"] for i in original["call"]["inputs"]
        ]
        assert copy_["outputs"] == original["outputs"]


def test_report_fixture_validates() -> None:
    """The within_tolerance report validates and names its tolerances."""
    report = json.loads((FIXTURES / "report_within_tolerance.json").read_text("utf-8"))
    validate_report(report["report"])
    assert report["report"]["rule"] == {"kind": "tolerance", "rtol": 1e-12, "atol": 0.0}


def test_block_locators_and_fingerprints() -> None:
    """Locators come from the written objects; fingerprints read with h5py."""
    sig = signal([-2.0, 0.0, 1.0, 4.0])
    ledger = Ledger()
    state_id = ledger.observe(OBJ_UUID, signal_state_facts(sig))
    with _memory_file() as h5file:
        _write_signal(h5file, "/DataLab_Sig/g001: Group/s001: S", sig, OBJ_UUID)
        assert scan_object_index(h5file)[OBJ_UUID]["path"] == (
            "/DataLab_Sig/g001: Group/s001: S"
        )
        locators = build_locators(h5file, {OBJ_UUID: state_id}, {state_id: "signal"})
        write_block(h5file, ledger, locators)
        restored, read_locators = read_block(h5file)
        fingerprints = locate_fingerprints(h5file, read_locators)
        assert (
            fingerprints[state_id] == restored.states[state_id]["fingerprint"]["value"]
        )
        h5file["/DataLab_Sig/g001: Group/s001: S/xydata"][1, 3] = 4.5
        altered = locate_fingerprints(h5file, read_locators)
        assert altered[state_id] != fingerprints[state_id]
        assert read_signal(h5file, read_locators[state_id]["path"])["xunit"] == "s"


def test_save_and_load_ledger() -> None:
    """Only current, unchanged states are located; load derives state statuses."""
    first, second, changed = (
        f"00000000-0000-4000-8000-00000000000{i}" for i in (2, 3, 4)
    )
    ledger = Ledger()
    old = ledger.observe(first, signal_state_facts(signal([1.0, 2.0, 3.0, 4.0])))
    current = ledger.observe(first, signal_state_facts(signal([1.0, 2.0, 3.0, 5.0])))
    kept = ledger.observe(second, signal_state_facts(signal([0.0, 1.0, 0.0, 1.0])))
    stale = ledger.observe(changed, signal_state_facts(signal([7.0, 7.0, 7.0, 7.0])))
    with _memory_file() as h5file:
        _write_signal(h5file, "/DataLab_Sig/g: G/a: A", signal([1, 2, 3, 5]), first)
        _write_signal(h5file, "/DataLab_Sig/g: G/b: B", signal([0, 1, 0, 1]), second)
        _write_signal(h5file, "/DataLab_Sig/g: G/c: C", signal([7, 7, 7, 8]), changed)
        assert set(locate_current_states(h5file, ledger)) == {current, kept}
        save_ledger(h5file, ledger)
        restored, status = load_ledger(h5file)
        assert restored.to_dict() == ledger.to_dict()
        assert status == {old: "unavailable", stale: "unavailable"}
        h5file["/DataLab_Sig/g: G/b: B/xydata"][1, 0] = 0.5
        assert load_ledger(h5file)[1][kept] == "altered"


def test_images_and_uncertainties_are_located() -> None:
    """Image states (uniform or not) and signals with uncertainty are located."""
    uniform, nonuniform, uncertain = (
        f"00000000-0000-4000-8000-00000000001{i}" for i in (1, 2, 3)
    )
    data = np.arange(6, dtype=np.uint16).reshape(2, 3)
    coords = (np.array([0.0, 1.0, 3.0]), np.array([0.0, 2.0]))
    sig = signal([1.0, 2.0, 3.0, 4.0], dy=np.full(4, 0.1))
    ledger = Ledger()
    states = {
        uniform: ledger.observe(uniform, state_facts(image(data, x0=1.0, dx=0.5))),
        nonuniform: ledger.observe(nonuniform, state_facts(image(data, coords=coords))),
        uncertain: ledger.observe(uncertain, state_facts(sig)),
    }
    with _memory_file() as h5file:
        _write_image(h5file, "/DataLab_Ima/g: G/i1: I", data, uniform, x0=1.0, dx=0.5)
        _write_image(h5file, "/DataLab_Ima/g: G/i2: J", data, nonuniform, coords)
        group = h5file.create_group("/DataLab_Sig/g: G/s1: S")
        group.attrs["xunit"], group.attrs["yunit"] = "s", ""
        group["xydata"] = np.vstack([sig.x, sig.y, np.full(4, np.nan), sig.dy])
        group.create_group("metadata").attrs["__uuid"] = uncertain
        assert set(locate_current_states(h5file, ledger)) == set(states.values())
        save_ledger(h5file, ledger)
        assert load_ledger(h5file)[1] == {}
        assert read_image(h5file, "/DataLab_Ima/g: G/i1: I")["dx"] == 0.5
        h5file["/DataLab_Ima/g: G/i2: J/data"][0, 0] = 9
        assert load_ledger(h5file)[1] == {states[nonuniform]: "altered"}


def test_absent_block() -> None:
    """A file without the block reports provenance as absent."""
    with _memory_file() as h5file:
        assert read_block(h5file) is None


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda g: g.attrs.__setitem__("schema_version", 2),
        lambda g: g.__delitem__("ledger_json"),
        lambda g: (
            g.__delitem__("ledger_json"),
            g.create_dataset("ledger_json", data="{not json"),
        ),
        lambda g: (
            g.__delitem__("locators_json"),
            g.create_dataset("locators_json", data='{"x": 1}'),
        ),
        lambda g: (
            g.__delitem__("locators_json"),
            g.create_dataset(
                "locators_json",
                data=json.dumps(
                    {
                        "00000000-0000-4000-8000-000000000001": {
                            "kind": "signal",
                            "path": "/etc/passwd/x",
                        }
                    }
                ),
            ),
        ),
    ],
)
def test_invalid_block_is_refused(corrupt) -> None:
    """Invalid blocks raise ProvenanceFormatError, never KeyError."""
    ledger = Ledger()
    ledger.observe(OBJ_UUID, signal_state_facts(signal([1.0, 2.0, 3.0, 4.0])))
    with _memory_file() as h5file:
        write_block(h5file, ledger, {})
        corrupt(h5file[BLOCK_GROUP])
        with pytest.raises(ProvenanceFormatError):
            read_block(h5file)


def test_oversized_json_text_is_refused(monkeypatch) -> None:
    """The JSON size limit applies to variable-length strings too."""
    ledger = Ledger()
    ledger.observe(OBJ_UUID, signal_state_facts(signal([1.0, 2.0, 3.0, 4.0])))
    with _memory_file() as h5file:
        write_block(h5file, ledger, {})
        assert h5file[BLOCK_GROUP]["ledger_json"].dtype.kind == "O"
        monkeypatch.setattr(hdf5_module, "MAX_JSON_BYTES", 64)
        with pytest.raises(ProvenanceFormatError, match="too large"):
            read_block(h5file)


def test_links_are_refused(tmp_path: Path) -> None:
    """External and soft links under the panel roots are refused."""
    other = tmp_path / "other.h5"
    with h5py.File(other, "w") as h5file:
        h5file.create_group("data")
    with h5py.File(tmp_path / "main.h5", "w") as h5file:
        h5file.create_group("DataLab_Sig/g001: Group")
        h5file["DataLab_Sig/g001: Group/s001: X"] = h5py.ExternalLink(
            str(other), "/data"
        )
        with pytest.raises(ProvenanceFormatError):
            scan_object_index(h5file)
    with h5py.File(tmp_path / "soft.h5", "w") as h5file:
        h5file.create_group("elsewhere")
        h5file.create_group("DataLab_Sig/g001: Group")
        h5file["DataLab_Sig/g001: Group/s001: X"] = h5py.SoftLink("/elsewhere")
        with pytest.raises(ProvenanceFormatError):
            scan_object_index(h5file)
