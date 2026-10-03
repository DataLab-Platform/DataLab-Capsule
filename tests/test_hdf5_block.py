# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Tests for the HDF5 provenance block and the expressiveness fixtures."""

from __future__ import annotations

import io
import json
from pathlib import Path

import h5py
import numpy as np
import pytest

from datalab_capsule.compare import validate_report
from datalab_capsule.hdf5 import (
    BLOCK_GROUP,
    ProvenanceFormatError,
    build_locators,
    locate_fingerprints,
    read_block,
    read_signal,
    scan_object_index,
    write_block,
)
from datalab_capsule.integrity import signal_state_facts
from datalab_capsule.ledger import Ledger

from .helpers import signal

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
