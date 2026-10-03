# Copyright (c) DataLab Platform Developers, BSD 3-Clause license, see LICENSE file.

"""Create a capsule from a DataLab workspace file, with DataLab-Capsule only.

Usage::

    python workspace_to_capsule.py WORKSPACE.h5 OUTPUT.dlcapsule

The workspace must have been saved by DataLab (Desktop or Web) with its
provenance ledger. Requires the ``hdf5`` extra (NumPy and h5py).
"""

from __future__ import annotations

import sys
from pathlib import Path

from datalab_capsule.archive import create_from_hdf5, read_capsule
from datalab_capsule.manifest import inspect_manifest


def main() -> None:
    """Write the capsule, read it back and print a short summary."""
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    target.write_bytes(create_from_hdf5(source, name=source.stem))
    summary = inspect_manifest(read_capsule(target).manifest)
    replayable = sum(a["replayable"] for a in summary["activities"])
    print(
        f"{target}: {len(summary['activities'])} activities "
        f"({replayable} replayable), {len(summary['states'])} states"
    )


if __name__ == "__main__":
    main()
