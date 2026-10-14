# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Write cross-edition verification reports of the families corpus sources.

Development script, run once in a DataLab development environment (the package
itself never imports DataLab), from the DataLab folder::

    python scripts/run_with_env.py python SCRIPT FAMILIES_WEB OUT_DIR

where ``SCRIPT`` is this file and ``FAMILIES_WEB`` is ``families_web.h5`` (the
families scenario saved by DataLab-Web in Pyodide). DataLab Desktop reopens it
and verifies three of its activities, in another environment and edition:

- ``report_families_convolution.json``: an image convolution reading a global
  option, recorded in the activity context and applied again;
- ``report_families_centroid.json``: an analysis result (geometry artifact),
  compared on its structure and values;
- ``report_families_detection.json``: a detection that found nothing, replayed
  without result.
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPORTS = {
    "sigima.image.convolution": "report_families_convolution.json",
    "sigima.image.centroid": "report_families_centroid.json",
    "sigima.image.peak_detection": "report_families_detection.json",
}


def main(families: str, out_dir: str) -> None:
    """Write the reports listed in the module docstring."""
    # pylint: disable=import-outside-toplevel
    from datalab.config import Conf
    from datalab.env import execenv
    from datalab.tests import datalab_test_app_context
    from guidata.qthelpers import qt_app_context

    execenv.unattended = True
    with qt_app_context(), Conf.process_isolation_enabled.context(False):
        with datalab_test_app_context(console=False, exec_loop=False) as win:
            win.load_h5_workspace([families], reset_all=True)
            for activity in win.provenance.ledger.activities:
                name = REPORTS.get(activity["call"]["operation"]["id"])
                if name is None:
                    continue
                report = win.verify_provenance_activity(activity["activity_id"])
                with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                    json.dump(report, f, indent=2, ensure_ascii=False)
                    f.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
