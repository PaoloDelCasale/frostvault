"""The installation's only principal (ADR-0017 and ADR-0035).

The database enforces this identity with ``owner.id CHECK (id = 1)``. Runtime
work never needs to look up a recipient/requester: every Vault, Session and
notification belongs to the Owner. Bootstrap creates its credentials before
services start.
"""

OWNER_ID = 1


def job_actor(job: dict) -> str:
    """Audit the initiating principal, not the worker executing its request."""
    return "system" if job.get("origin") == "automatic" else "owner"
