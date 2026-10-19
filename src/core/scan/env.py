# -*- coding: utf-8 -*-
"""Environment placeholder expansion for catalog paths.

The catalog never contains a hard-coded user name or drive letter; every path
starts with ``%APPDATA%``, ``%USERPROFILE%`` or ``%LOCALAPPDATA%``.  Lookup is
case-insensitive on every platform so that an injected environment mapping
behaves the same way as Windows' native ``os.environ``.

Python 3.8 compatible (decision 0005).
"""

import os
import re
from typing import Dict, Mapping, Optional

ENV_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


class UnknownEnvironmentVariable(Exception):
    """Raised when a placeholder such as ``%APPDATA%`` cannot be resolved."""

    def __init__(self, name):
        # type: (str) -> None
        super(UnknownEnvironmentVariable, self).__init__(name)
        self.name = name


def normalise_environment(environ):
    # type: (Optional[Mapping[str, str]]) -> Dict[str, str]
    """Return an upper-case keyed copy of ``environ`` for case-insensitive use."""
    source = os.environ if environ is None else environ
    return {str(key).upper(): str(value) for key, value in source.items()}


def expand_environment(template, environ=None):
    # type: (str, Optional[Mapping[str, str]]) -> str
    """Replace ``%VAR%`` tokens with values from ``environ``.

    Raises ``UnknownEnvironmentVariable`` instead of silently producing an
    invalid path, so callers can report a warning and mark the entry absent.
    """
    lookup = normalise_environment(environ)

    def replace(match):
        # type: (re.Match) -> str
        name = match.group(1)
        value = lookup.get(name.upper())
        if value is None:
            raise UnknownEnvironmentVariable(name)
        return value

    return ENV_VAR_RE.sub(replace, template)


def find_placeholders(template):
    # type: (str) -> list
    """Return the placeholder names used by ``template`` (for diagnostics)."""
    return [match.group(1) for match in ENV_VAR_RE.finditer(template)]
