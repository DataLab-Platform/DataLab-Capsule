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
    CAPSULE_ORIGIN_DATASET,
    ProvenanceFormatError,
    build_locators,
    load_ledger,
    locate_current_states,
    locate_fingerprints,
    read_block,
    read_capsule_origin,
    read_image,
    read_roi,
    read_signal,
    save_ledger,
    scan_object_index,
    write_block,
)
from datalab_capsule.integrity import signal_state_facts, state_facts, value_digest
from datalab_capsule.ledger import Ledger

from .helpers import ROI, image, signal, write_metadata

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


def test_float32_and_complex_states_are_located() -> None:
    """Float32 and complex signals and complex images match their saved rows.

    Sigima stores signal rows in one ``xydata`` array of the value dtype and
    exposes ``x`` as real float64: both give the same fingerprint record.
    """
    ids = [f"00000000-0000-4000-8000-00000000002{i}" for i in range(4)]
    x = np.array([0.0, 0.5, 1.0, 1.5])
    stored = {
        ids[0]: np.vstack([x, [1.0, 2.0, 3.0, 4.0]]).astype(np.float32),
        ids[1]: np.vstack([x, [1j, 2.0, 3 - 1j, 4.0]]).astype(np.complex128),
        ids[2]: np.vstack(
            [x, [1j, 2.0, 3.0, 4.0], np.full(4, np.nan), np.full(4, 0.5)]
        ).astype(np.complex64),
    }
    data = (np.arange(6).reshape(2, 3) * (1 - 2j)).astype(np.complex128)
    ledger = Ledger()
    states = {}
    for object_uuid, xydata in stored.items():
        dy = xydata[3] if xydata.shape[0] == 4 else None
        live = signal(
            [0.0] * 4, x=xydata[0].real.astype(float), xunit="s", yunit="V", dy=dy
        )
        live.y = xydata[1]
        facts = signal_state_facts(live)
        assert facts["fingerprint"]["scheme"] == "datalab-signal-v2"
        states[object_uuid] = ledger.observe(object_uuid, facts)
    states[ids[3]] = ledger.observe(ids[3], state_facts(image(data)))
    with _memory_file() as h5file:
        for index, (object_uuid, xydata) in enumerate(stored.items()):
            group = h5file.create_group(f"/DataLab_Sig/g: G/s{index}: S")
            group.attrs["xunit"], group.attrs["yunit"] = "s", "V"
            group["xydata"] = xydata
            group.create_group("metadata").attrs["__uuid"] = object_uuid
        _write_image(h5file, "/DataLab_Ima/g: G/i: I", data, ids[3])
        assert set(locate_current_states(h5file, ledger)) == set(states.values())
        save_ledger(h5file, ledger)
        assert load_ledger(h5file)[1] == {}
        h5file["/DataLab_Sig/g: G/s1: S/xydata"][1, 0] = -1j
        h5file["/DataLab_Ima/g: G/i: I/data"][0, 1] = 1 + 2j
        assert load_ledger(h5file)[1] == {
            states[ids[1]]: "altered",
            states[ids[3]]: "altered",
        }


def test_absent_block() -> None:
    """A file without the block reports provenance as absent."""
    with _memory_file() as h5file:
        assert read_block(h5file) is None


IMAGE_PATH = "/DataLab_Ima/g: G/i1: I"


def _saved_image_with_roi(h5file, roi: ROI | None) -> tuple[Ledger, str]:
    """Save one calibrated image (with *roi*) and its ledger into *h5file*."""
    data = np.arange(12, dtype=np.float32).reshape(3, 4)
    ledger = Ledger()
    state_id = ledger.observe(
        OBJ_UUID, state_facts(image(data, x0=1.0, dx=0.5, roi=roi))
    )
    _write_image(h5file, IMAGE_PATH, data, OBJ_UUID, x0=1.0, dx=0.5)
    if roi is not None:
        write_metadata(h5file[IMAGE_PATH]["metadata"], "_roi_", roi.to_dict())
    save_ledger(h5file, ledger)
    return ledger, state_id


def test_roi_is_read_back_from_the_file() -> None:
    """The ROI read from HDF5 has the digest recorded from the live object."""
    roi = ROI([0.0, 0.0, 2.0, 2.0], kind="RectangularROI")
    with _memory_file() as h5file:
        ledger, state_id = _saved_image_with_roi(h5file, roi)
        assert read_roi(h5file, IMAGE_PATH) is not None
        assert (
            value_digest(read_roi(h5file, IMAGE_PATH))
            == (ledger.states[state_id]["roi"]["digest"])
        )
        assert load_ledger(h5file)[1] == {}


def _shift_roi(h5file) -> None:
    h5file[f"{IMAGE_PATH}/metadata/_roi_/single_rois/__seq0/coords"][2] = 3.0


def _drop_roi(h5file) -> None:
    del h5file[f"{IMAGE_PATH}/metadata/_roi_"]


def _invert_roi(h5file) -> None:
    h5file[f"{IMAGE_PATH}/metadata/_roi_/single_rois/__seq0"].attrs["inverse"] = True


@pytest.mark.parametrize("change", [_shift_roi, _drop_roi, _invert_roi])
def test_roi_change_in_file_is_altered(change) -> None:
    """Unchanged pixels with a different ROI do not make the state intact."""
    roi = ROI([0.0, 0.0, 2.0, 2.0], kind="RectangularROI")
    with _memory_file() as h5file:
        _ledger, state_id = _saved_image_with_roi(h5file, roi)
        change(h5file)
        assert load_ledger(h5file)[1] == {state_id: "altered"}


def test_roi_added_in_file_is_altered() -> None:
    """A ROI added to an object recorded without ROI alters its state."""
    with _memory_file() as h5file:
        _ledger, state_id = _saved_image_with_roi(h5file, None)
        roi = ROI([0.0, 0.0, 2.0, 2.0], kind="RectangularROI")
        write_metadata(h5file[IMAGE_PATH]["metadata"], "_roi_", roi.to_dict())
        assert load_ledger(h5file)[1] == {state_id: "altered"}


def _set_attr(name: str, value):
    def change(h5file) -> None:
        h5file[IMAGE_PATH].attrs[name] = value

    change.__name__ = f"{name}={value}"
    return change


def _make_nonuniform(h5file) -> None:
    group = h5file[IMAGE_PATH]
    group.attrs["is_uniform_coords"] = False
    del group["xcoords"], group["ycoords"]
    group["xcoords"] = 1.0 + 0.5 * np.arange(4.0)
    group["ycoords"] = np.arange(3.0)


@pytest.mark.parametrize(
    "change",
    [
        _set_attr("x0", 1.25),
        _set_attr("dy", 2.0),
        _set_attr("xunit", "um"),
        _set_attr("zunit", ""),
        _make_nonuniform,
    ],
    ids=lambda c: c.__name__,
)
def test_calibration_or_unit_change_in_file_is_altered(change) -> None:
    """Image calibration and units belong to the state, not only the pixels."""
    with _memory_file() as h5file:
        _ledger, state_id = _saved_image_with_roi(h5file, None)
        change(h5file)
        assert load_ledger(h5file)[1] == {state_id: "altered"}


def test_roi_or_calibration_change_in_memory_is_a_new_state() -> None:
    """A ROI-only or calibration-only change creates a new state on observe."""
    data = np.ones((2, 2))
    roi = ROI([0.0, 0.0, 1.0, 1.0], kind="RectangularROI")
    ledger = Ledger()
    base = ledger.observe(OBJ_UUID, state_facts(image(data)))
    with_roi = ledger.observe(OBJ_UUID, state_facts(image(data, roi=roi)))
    moved = ledger.observe(OBJ_UUID, state_facts(image(data, x0=0.5, roi=roi)))
    assert len({base, with_roi, moved}) == 3
    assert ledger.states[base]["fingerprint"] == ledger.states[with_roi]["fingerprint"]
    with _memory_file() as h5file:
        _write_image(h5file, IMAGE_PATH, data, OBJ_UUID, x0=0.5)
        write_metadata(h5file[IMAGE_PATH]["metadata"], "_roi_", roi.to_dict())
        assert set(locate_current_states(h5file, ledger)) == {moved}
        # Earlier states of the same pixels are not located as current.
        h5file[IMAGE_PATH].attrs["x0"] = 0.0
        assert locate_current_states(h5file, ledger) == {}


def test_metadata_decoder_is_bounded(monkeypatch) -> None:
    """An oversized ROI definition is refused rather than decoded."""
    roi = ROI([0.0, 0.0, 2.0, 2.0], kind="RectangularROI")
    with _memory_file() as h5file:
        _saved_image_with_roi(h5file, roi)
        monkeypatch.setattr(hdf5_module, "MAX_ROI_NODES", 2)
        with pytest.raises(ProvenanceFormatError, match="too large"):
            read_roi(h5file, IMAGE_PATH)


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


ORIGIN = {
    "fingerprint": "sha256:" + "a" * 64,
    "name": "parent",
    "attachments": ["protocol.txt"],
}


def test_capsule_origin_is_saved_with_the_block() -> None:
    """The capsule origin is written when known and read back checked."""
    ledger = Ledger()
    with _memory_file() as h5file:
        save_ledger(h5file, ledger)
        assert CAPSULE_ORIGIN_DATASET not in h5file[BLOCK_GROUP]
        assert read_capsule_origin(h5file) is None
        save_ledger(h5file, ledger, ORIGIN)
        assert read_capsule_origin(h5file) == ORIGIN
        # Readers of the ledger alone are not affected.
        assert load_ledger(h5file) is not None
    with _memory_file() as h5file:
        assert read_capsule_origin(h5file) is None


@pytest.mark.parametrize(
    "origin",
    [
        {**ORIGIN, "fingerprint": "sha256:xyz"},
        {**ORIGIN, "name": 3},
        {**ORIGIN, "attachments": "protocol.txt"},
        {**ORIGIN, "attachments": [1]},
        {**ORIGIN, "extra": True},
        {"fingerprint": ORIGIN["fingerprint"]},
        [ORIGIN],
    ],
)
def test_invalid_capsule_origin_is_refused(origin) -> None:
    """An invalid origin is never written, and refused when read."""
    with _memory_file() as h5file:
        with pytest.raises(ProvenanceFormatError):
            write_block(h5file, Ledger(), {}, origin)
        write_block(h5file, Ledger(), {})
        h5file[BLOCK_GROUP].create_dataset(
            CAPSULE_ORIGIN_DATASET,
            data=json.dumps(origin),
            dtype=h5py.string_dtype("utf-8"),
        )
        with pytest.raises(ProvenanceFormatError):
            read_capsule_origin(h5file)
    with _memory_file() as h5file:
        write_block(h5file, Ledger(), {})
        h5file[BLOCK_GROUP].create_dataset(
            CAPSULE_ORIGIN_DATASET, data="{", dtype=h5py.string_dtype("utf-8")
        )
        with pytest.raises(ProvenanceFormatError):
            read_capsule_origin(h5file)


def test_malformed_objects_are_refused() -> None:
    """Malformed object nodes give a format error, never another exception."""
    sig, ima = "/DataLab_Sig/g", "/DataLab_Ima/g"
    with _memory_file() as h5file:
        h5file[f"{sig}/dataset"] = np.zeros(3)
        h5file["/DataLab_Sig/flat"] = np.zeros(3)
        h5file[f"{sig}/text/xydata"] = np.array([[b"a", b"b"], [b"c", b"d"]])
        h5file.create_group(f"{ima}/origin").attrs["x0"] = "not a number"
        h5file[f"{ima}/origin/data"] = np.zeros((2, 2))
        h5file.create_group(f"{sig}/roi")["metadata"] = np.zeros(1)
        h5file.create_group(f"{sig}/units").attrs["xunit"] = np.bytes_(b"\xff")
        h5file[f"{sig}/units/xydata"] = np.zeros((2, 3))
        for reader, path in (
            (read_signal, f"{sig}/dataset"),
            (read_signal, "/DataLab_Sig/flat/inner"),
            (read_signal, f"{sig}/text"),
            (read_image, f"{ima}/origin"),
            (read_roi, f"{sig}/roi"),
            (read_signal, f"{sig}/units"),
        ):
            with pytest.raises(ProvenanceFormatError):
                reader(h5file, path)
        located = {"s": {"kind": "signal", "path": f"{sig}/text"}}
        with pytest.raises(ProvenanceFormatError):
            locate_fingerprints(h5file, located)


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
