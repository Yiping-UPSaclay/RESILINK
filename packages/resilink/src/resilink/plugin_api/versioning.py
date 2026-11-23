"""Version helpers for the plugin API: SemVer identities and PEP 440 ranges (blueprint §6.13, §6.14)."""

from __future__ import annotations

import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

PLUGIN_API_VERSION = "0.1.0"  # draft until two independent theses have used it (ADR-0008 §10)

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.\-+]+)?$")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_semver(version: str) -> bool:
    return bool(SEMVER_RE.match(version))


def check_semver(version: str, what: str = "version") -> str:
    if not is_semver(version):
        raise ValueError(f"{what} {version!r} is not a SemVer version such as '0.1.0'")
    return version


def check_id(value: str, what: str = "id") -> str:
    if not ID_RE.match(value):
        raise ValueError(f"{what} {value!r} must be lower-case letters, digits and '_' and start with a letter")
    return value


def check_spec(spec: str, what: str = "version range") -> str:
    try:
        SpecifierSet(spec)
    except InvalidSpecifier as exc:
        raise ValueError(f"{what} {spec!r} is not a valid PEP 440 range such as '>=0.1,<0.2'") from exc
    return spec


def satisfies(version: str, spec: str) -> bool:
    """True when ``version`` lies in the PEP 440 range ``spec`` (pre-releases allowed)."""
    try:
        return SpecifierSet(spec).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        return False
