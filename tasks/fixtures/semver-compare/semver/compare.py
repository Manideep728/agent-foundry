"""Ordering semantic versions."""

import re

_PATTERN = re.compile(r"\A(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?\Z")


def parse(version):
    """Split a version into (major, minor, patch, prerelease).

    The first three are integers. The prerelease is the text after the hyphen,
    or None when there is none.
    """
    match = _PATTERN.match(version)
    if match is None:
        raise ValueError(f"not a version: {version!r}")
    major, minor, patch, prerelease = match.groups()
    return int(major), int(minor), int(patch), prerelease


def compare(a, b):
    """Return -1, 0 or 1 as a sorts below, equal to, or above b.

    major, minor and patch are compared as numbers, most significant first. A
    version carrying a prerelease sorts below the same version without one,
    because a prerelease is what precedes the release. Two prereleases are
    compared identifier by identifier: a numeric identifier is compared as a
    number and sorts below an alphanumeric one, and when one side runs out of
    identifiers first the shorter sorts below.
    """
    major_a, minor_a, patch_a, _pre_a = parse(a)
    major_b, minor_b, patch_b, _pre_b = parse(b)

    for left, right in ((major_a, major_b), (minor_a, minor_b), (patch_a, patch_b)):
        if left != right:
            return -1 if left < right else 1
    return 0
