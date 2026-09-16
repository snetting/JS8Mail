"""Conservative JS8Call group catalog and observation helpers."""

from __future__ import annotations

import re

DEFAULT_GROUPS: tuple[tuple[str, str], ...] = (
    ("@JS8MAIL", "discussion and updates"),
    ("@EMCOMM", "emergency comms"),
    ("@ARES", "emergency comms"),
    ("@RACES", "emergency comms"),
    ("@RAYNET", "emergency comms"),
    ("@NTS", "traffic handling"),
    ("@JS8NET", "JS8 network services"),
    ("@SKYWARN", "weather spotting"),
    ("@WX", "weather"),
    ("@AMRRON", "emergency preparedness"),
    ("@DX/EU", "continental Europe"),
    ("@DX/NA", "continental North America"),
    ("@DX/SA", "continental South America"),
    ("@DX/AS", "continental Asia"),
    ("@DX/AF", "continental Africa"),
    ("@DX/OC", "continental Oceania"),
    ("@DX/AN", "continental Antarctica"),
)

_GROUP_RE = re.compile(r"(?<![A-Z0-9])@[A-Z0-9][A-Z0-9/_-]{1,15}\b", re.IGNORECASE)


def extract_groups(*values: str) -> tuple[str, ...]:
    found: set[str] = set()
    for value in values:
        found.update(match.group(0).upper() for match in _GROUP_RE.finditer(value))
    return tuple(sorted(found))


def default_group_description(group: str) -> str:
    wanted = group.upper()
    return next(
        (description for name, description in DEFAULT_GROUPS if name == wanted), "observed group"
    )
