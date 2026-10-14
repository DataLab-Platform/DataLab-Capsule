# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""The same scenario recorded by DataLab Desktop and by DataLab-Web.

``families_desktop.h5`` and ``families_web.h5`` (corpus sources) were saved by
each edition after the same scenario: one operation of each family. Their
calls, parameters, roles, input states and contexts are identical; only the
edition, the environment, the identifiers and the dates differ.
"""

from __future__ import annotations

from typing import Any

import h5py

from datalab_capsule.hdf5 import read_block

from .test_corpus import CORPUS


def _scenario(name: str) -> tuple[list[dict[str, Any]], set[str]]:
    """Return the activities of a workspace without identifiers, and editions."""
    with h5py.File(CORPUS / "sources" / name, "r") as h5file:
        ledger, _locators = read_block(h5file)
    states = ledger.states

    def state(state_id: str) -> dict[str, Any]:
        facts = states[state_id]
        return {k: facts.get(k) for k in ("kind", "fingerprint", "roi", "dtype")}

    activities = []
    for activity in ledger.activities:
        call = activity["call"]
        activities.append(
            {
                "operation": call["operation"],
                "parameters": call["parameters"],
                "inputs": [
                    (item["role"], state(item["binding"]["state_id"]))
                    for item in call["inputs"]
                ],
                "outputs": [
                    (o["role"], o["artifact"]["kind"])
                    if "artifact" in o
                    else (o["role"], states[o["state_id"]]["kind"])
                    for o in activity["outputs"]
                ],
                "context": activity["context"],
                "limits": activity["limits"],
                "origin": activity["origin"],
            }
        )
    return activities, {a["edition"] for a in ledger.activities}


def test_same_scenario_gives_the_same_calls() -> None:
    """Calls, parameters, roles, input states and contexts match."""
    desktop, desktop_editions = _scenario("families_desktop.h5")
    web, web_editions = _scenario("families_web.h5")
    assert (desktop_editions, web_editions) == ({"desktop"}, {"web"})
    assert len(desktop) == len(web) == 10
    for recorded_by_desktop, recorded_by_web in zip(desktop, web):
        assert recorded_by_desktop == recorded_by_web
    assert all(a["operation"] is not None and not a["limits"] for a in web)
    assert web[-1]["outputs"] == []  # a detection that found nothing
