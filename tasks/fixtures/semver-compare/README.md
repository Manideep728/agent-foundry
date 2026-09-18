# semver

A minimal semantic-version comparator.

`semver.compare.compare(a, b)` returns `-1`, `0` or `1`, so it can be handed to
`functools.cmp_to_key` and used for sorting.

Rules it is meant to honour:

- `major`, `minor` and `patch` are compared as numbers, most significant
  first. `1.10.0` is above `1.9.0`, because ten is above nine.
- **A version carrying a prerelease is below the same version without one.**
  `1.0.0-alpha` comes before `1.0.0`, because a prerelease is what precedes
  the release.
- Two prereleases are compared identifier by identifier, splitting on dots. A
  purely numeric identifier is compared as a number, and sorts below an
  alphanumeric one. When one runs out of identifiers first, the shorter is
  below: `1.0.0-alpha` is below `1.0.0-alpha.1`.
- Anything that is not `major.minor.patch`, optionally followed by `-` and a
  prerelease, is a `ValueError`.
