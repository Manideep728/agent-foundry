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


def _compare_identifiers(left, right):
    left_numeric, right_numeric = left.isdigit(), right.isdigit()
    if left_numeric and right_numeric:
        if int(left) == int(right):
            return 0
        return -1 if int(left) < int(right) else 1
    if left_numeric != right_numeric:
        return -1 if left_numeric else 1
    if left == right:
        return 0
    return -1 if left < right else 1


def _compare_prerelease(left, right):
    if left is None and right is None:
        return 0
    if left is None:
        return 1
    if right is None:
        return -1

    left_parts = left.split(".")
    right_parts = right.split(".")
    for left_part, right_part in zip(left_parts, right_parts):
        verdict = _compare_identifiers(left_part, right_part)
        if verdict:
            return verdict
    if len(left_parts) == len(right_parts):
        return 0
    return -1 if len(left_parts) < len(right_parts) else 1


def compare(a, b):
    """Return -1, 0 or 1 as a sorts below, equal to, or above b.

    major, minor and patch are compared as numbers, most significant first. A
    version carrying a prerelease sorts below the same version without one,
    because a prerelease is what precedes the release. Two prereleases are
    compared identifier by identifier: a numeric identifier is compared as a
    number and sorts below an alphanumeric one, and when one side runs out of
    identifiers first the shorter sorts below.
    """
    major_a, minor_a, patch_a, pre_a = parse(a)
    major_b, minor_b, patch_b, pre_b = parse(b)

    for left, right in ((major_a, major_b), (minor_a, minor_b), (patch_a, patch_b)):
        if left != right:
            return -1 if left < right else 1
    return _compare_prerelease(pre_a, pre_b)
