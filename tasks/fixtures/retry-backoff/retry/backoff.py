"""The wait schedule between retry attempts."""


def delays(attempts, base_ms, cap_ms):
    """Return the waits to insert between attempts, in milliseconds.

    The waits sit between attempts, so `attempts` tries need `attempts - 1`
    waits and a single attempt waits not at all. Each wait doubles the one
    before it, starting at base_ms, and no wait ever exceeds cap_ms: once the
    doubling reaches the cap, every later wait stays at the cap. Every wait is
    a whole number of milliseconds.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if base_ms <= 0 or cap_ms <= 0:
        raise ValueError("base_ms and cap_ms must be positive")
    waits = []
    for index in range(attempts - 1):
        waits.append(base_ms * (2**index))
    return waits
