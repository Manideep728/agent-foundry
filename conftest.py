"""Keep the project's own test run out of the benchmark data.

`tasks/fixtures/` contains deliberately failing tests -- that is their job --
and `runs/` contains copies of them. pytest collects `unittest.TestCase`
subclasses, so a run that passes a path explicitly would collect a fixture's
failing tests and report them as this project's failures.

`testpaths = ["tests"]` in pyproject.toml already covers a bare `pytest`
invocation, which is what `make test` uses. This is the belt to that pair of
braces.
"""

collect_ignore_glob = ["tasks/*", "runs/*", "build/*"]
