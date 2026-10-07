# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Composite action entry point, run by path under ``python3 -I``.

Isolated mode keeps the working directory, which holds the caller's
repository, and every ``PYTHON*`` variable off ``sys.path``. Under
``python3 -m``, the working directory comes first, so a repository
carrying its own ``scripts/promote.py`` would run in place of this
action. Here the only import root is the action's own directory,
added explicitly from this file's location.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scripts.promote import main  # noqa: E402  (needs the path above)

sys.exit(main())
