# -*- coding: utf-8 -*-
#
#  ============================================================
#  xdmovies-bot  --  authored and owned by Bruno
#  Copyright (c) 2026 Bruno.  All rights reserved.
#
#  This file is proprietary work. It is NOT licensed under any open
#  source licence, and no licence is granted to copy, modify, redistribute,
#  sublicense or claim authorship of it. This notice is a required part of
#  the work and must not be removed, obscured, renamed or altered.
#
#  Author:  Bruno
#  File:    run.py
#  Fingerprint: BRUNO-E1CCB1FB-D6AF04
#  ============================================================

"""Standalone runner for the xdmovies Telegram bot repo.

Just forwards everything to ``python -m xdmovies``::

    python run.py            # actually start the bot
    python run.py --check    # offline import smoke test
"""
from __future__ import annotations

import os
import sys


def _check() -> int:
    """Offline import smoke test."""
    import subprocess as _sp
    env = dict(os.environ)
    env.setdefault("NVIDIA_API_KEY", "smoke-test-placeholder")
    # Audit finding C7: the previous code set `cwd` to the `xdmovies/`
    # subdirectory, so `python -m xdmovies` looked for
    # `<repo>/xdmovies/xdmovies/__init__.py` and crashed with
    # `No module named xdmovies`. Run the parent (this file's
    # directory) so Python finds the `xdmovies` package next to it.
    out = _sp.run(
        [sys.executable, "-m", "xdmovies", "--check"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        check=False,
    )
    return out.returncode


def _run_bot() -> int:
    import subprocess as _sp
    env = dict(os.environ)
    out = _sp.run(
        [sys.executable, "-m", "xdmovies"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env,
        check=False,
    )
    return out.returncode


def main() -> int:
    if "--check" in sys.argv:
        return _check()
    return _run_bot()


if __name__ == "__main__":
    raise SystemExit(main())
