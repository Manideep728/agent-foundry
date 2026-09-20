"""AgentFoundry control plane.

Layers, with dependencies pointing inward:

    interface -> services -> domain
    adapters  -> (implement interfaces defined by domain/services)

`domain` must not import anything that touches the filesystem, a database,
Docker, a model API, environment variables, or the system clock. That rule is
enforced by tests/test_domain_boundary.py, not by convention.
"""

__version__ = "0.1.0"
