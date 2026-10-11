# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Write the Desktop sources of the corpus fixtures (workspaces and reports).

Development script, run once in a DataLab development environment (the package
itself never imports DataLab), from the DataLab folder::

    python scripts/run_with_env.py python SCRIPT LEGACY DIFFERENCE OUT_DIR

where ``SCRIPT`` is this file, ``LEGACY`` is DataLab's
``provenance_legacy_1.3.0.h5`` test file (saved by the released DataLab 1.3.0,
without provenance block) and ``DIFFERENCE`` its
``provenance_desktop_difference.h5`` (A - B and B - A). It writes:

- ``legacy_imported.h5``: the legacy workspace opened (records imported as
  ``legacy_metadata`` activities) and saved with its provenance block;
- ``legacy_continued.h5``: the same workspace after one ordinary processing
  (``normalize`` of the moving average result), saved again;
- ``report_difference_exact.json``: verification of A - B (exact replay);
- ``report_difference_altered.json``: verification of A - B after its saved
  result was changed in the file (reference altered, not verified);
- ``report_legacy_opaque.json``: verification of a legacy activity (refused).
"""

from __future__ import annotations

import json
import os
import os.path as osp
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _write(path: str, report: dict) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
        file.write("\n")


def main(legacy: str, difference: str, out_dir: str) -> None:
    """Write the workspaces and reports listed in the module docstring."""
    # pylint: disable=import-outside-toplevel
    import h5py
    import sigima.params as sp
    import sigima.proc.signal as sips
    from datalab.config import Conf
    from datalab.env import execenv
    from datalab.objectmodel import get_uuid
    from datalab.tests import datalab_test_app_context
    from guidata.qthelpers import qt_app_context

    execenv.unattended = True
    altered = osp.join(out_dir, "difference_altered.h5")
    shutil.copy(difference, altered)
    with qt_app_context(), Conf.process_isolation_enabled.context(False):
        with datalab_test_app_context(console=False, exec_loop=False) as win:
            win.load_h5_workspace([legacy], reset_all=True)
            win.save_h5_workspace(osp.join(out_dir, "legacy_imported.h5"))
            activity = win.provenance.ledger.activities[0]
            report = win.verify_provenance_activity(activity["activity_id"])
            _write(osp.join(out_dir, "report_legacy_opaque.json"), report)
            source = next(
                obj
                for obj in win.signalpanel.objmodel
                if obj.title.startswith("moving")
            )
            win.signalpanel.objview.select_objects([get_uuid(source)])
            win.signalpanel.processor.compute_1_to_1(
                sips.normalize, sp.NormalizeParam.create(), edit=False
            )
            win.save_h5_workspace(osp.join(out_dir, "legacy_continued.h5"))

            win.load_h5_workspace([difference], reset_all=True)
            activity = win.provenance.ledger.activities[0]
            report = win.verify_provenance_activity(activity["activity_id"])
            _write(osp.join(out_dir, "report_difference_exact.json"), report)
            result_uuid = win.provenance.ledger.states[
                activity["outputs"][0]["state_id"]
            ]["object_uuid"]
    with h5py.File(altered, "r+") as h5file:
        for group in h5file["DataLab_Sig"].values():
            for node in group.values():
                if node["metadata"].attrs.get("__uuid") == result_uuid:
                    node["xydata"][1, 0] += 1.0
    with qt_app_context(), Conf.process_isolation_enabled.context(False):
        with datalab_test_app_context(console=False, exec_loop=False) as win:
            win.load_h5_workspace([altered], reset_all=True)
            report = win.verify_provenance_activity(activity["activity_id"])
            _write(osp.join(out_dir, "report_difference_altered.json"), report)
    os.remove(altered)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
