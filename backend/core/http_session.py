"""
Shared HTTP session with automatic retry/back-off.

All library client modules (drap_client, rxnorm_client, openfda_client)
import ``SESSION`` from here so every external HTTP call uses the same
connection pool and retry configuration.

Retry policy (unchanged from the original create_dataset.py):
  - Up to 3 total attempts (initial + 2 retries)
  - Back-off: 1 s, 2 s, 4 s between attempts
  - Retried status codes: 429, 500, 502, 503, 504
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_session() -> requests.Session:
    """Build and return a ``requests.Session`` with retry/back-off configured."""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,          # wait 1 s, 2 s, 4 s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


#: Module-level singleton — import this directly in client modules.
SESSION: requests.Session = build_session()
