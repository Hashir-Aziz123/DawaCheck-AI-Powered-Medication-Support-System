"""
Per-service rate limiting for outbound HTTP calls.

``rate_limit(service)`` enforces a minimum inter-call gap for each named
external service, blocking the current thread for the remainder of the
required delay if necessary.

Rate limits (seconds between calls) — unchanged from create_dataset.py:
    drap    1.5 s
    rxnorm  0.5 s
    openfda 0.5 s

All other service names default to 1.0 s.
"""

import time

#: Minimum gap (seconds) between successive calls to each service.
RATE_LIMIT_SECONDS: dict[str, float] = {
    "drap": 1.5,
    "rxnorm": 0.5,
    "openfda": 0.5,
}

#: Tracks the ``time.time()`` of the most recent call to each service.
_last_call: dict[str, float] = {}


def rate_limit(service: str) -> None:
    """
    Block until the minimum inter-call gap for *service* has elapsed.

    ``service`` must be one of the keys in ``RATE_LIMIT_SECONDS``; unknown
    services default to a 1.0 s gap.
    """
    delay = RATE_LIMIT_SECONDS.get(service, 1.0)
    elapsed = time.time() - _last_call.get(service, 0)
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_call[service] = time.time()
