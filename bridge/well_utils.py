"""Well-name normalization helpers for the Wallac Victor2 bridge.

Pure, dependency-free helpers extracted from :mod:`bridge.execution` to
break the analysis <-> execution import cycle. See the CodeQL
``py/cyclic-import`` findings (analysis.py:263, execution.py:45).
"""

from __future__ import annotations

import re
from typing import Any

_WELL_NAME_RE = re.compile(r"^([A-H])(\d{1,2})$")


def normalize_well_name(name: str) -> str:
    """Normalize a well name to canonical form (e.g. ``A01`` -> ``A1``).

    The vm-agent returns zero-padded names (``A01`` ... ``A12``), while
    layout/analysis specs use non-padded names (``A1`` ... ``A12``).
    Normalize to non-padded form for consistent comparison.
    """
    if not name:
        return ""
    m = _WELL_NAME_RE.match(name.upper().strip())
    if m:
        return f"{m.group(1)}{int(m.group(2))}"
    return name


def well_key(w: dict[str, Any]) -> str:
    """Extract and normalize the well address from a raw result dict.

    The vm-agent uses ``well``, layout/analysis specs use ``well_name``.
    Normalizes to non-padded form (``A1``, not ``A01``).
    """
    return normalize_well_name(w.get("well_name") or w.get("well") or "")
