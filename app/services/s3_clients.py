"""Process-local, bounded S3 clients; transfer state belongs to the caller facade.

Client construction is serialized, including Session creation (boto3's default
Session is neither a safe concurrent factory nor a fresh environment view).
Only the public SDK client is shared; never register per-transfer event handlers
or mutate client metadata. Reads/same-MPU parts retry; ambiguous mutators do not.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import threading
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from s3transfer.utils import signal_not_transferring, signal_transferring

_DEFAULT_FACTORY = boto3.client
_MAX_CLIENTS = 12
_lock = threading.Lock()
_clients: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
_live_clients: weakref.WeakSet = weakref.WeakSet()
_upload_setup_lock = threading.Lock()
_upload_ready: weakref.WeakSet = weakref.WeakSet()
# Keep inherited pools alive but never use/close them in the child, even via GC.
_inherited: list[Any] = []


def _after_fork() -> None:
    global _lock, _clients, _live_clients, _upload_setup_lock, _upload_ready
    _inherited.append((_clients, tuple(_live_clients), tuple(_upload_ready)))
    _lock = threading.Lock()
    _clients = OrderedDict()
    _live_clients = weakref.WeakSet()
    _upload_setup_lock = threading.Lock()
    _upload_ready = weakref.WeakSet()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def configure_upload_client(client: Any) -> None:
    """Install stateless SDK stream signals ONCE, never a transfer's callbacks.

    Also supports injected SDK clients at the upload_file seam. Shared clients
    from the factory are already configured before publication to any caller.
    """
    if not isinstance(client, BaseClient):
        return
    with _upload_setup_lock:
        if client not in _upload_ready:
            client.meta.events.register_first("request-created.s3", signal_not_transferring,
                                              unique_id="s3upload-not-transferring")
            client.meta.events.register_last("request-created.s3", signal_transferring,
                                             unique_id="s3upload-transferring")
            _upload_ready.add(client)


def _configuration_key() -> bytes:
    # Hash rather than retaining secret values in inspectable cache keys. Include
    # endpoint/proxy/profile/CA overrides: tests and operators can change these.
    values = sorted((name, value) for name, value in os.environ.items()
                    if name.startswith(("AWS_", "BOTO_")) or name in {
                        "HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                        "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
                    })
    for name, default in (("AWS_CONFIG_FILE", "~/.aws/config"),
                          ("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials"),
                          ("BOTO_CONFIG", "~/.boto")):
        path = Path(os.environ.get(name, default)).expanduser()
        try:
            info = path.stat()
            stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except OSError:
            stamp = None
        values.append((name + ":file", str(stamp)))
    return hashlib.sha256(repr(values).encode()).digest()


def s3_client(*, region: str, preflight: bool = False, non_idempotent: bool = False,
              factory: Callable[..., Any] = _DEFAULT_FACTORY) -> Any:
    """Return a shared client for this effective configuration and retry policy.

    ``factory`` preserves the existing app.storage.boto3.client override seam.
    The real SDK uses a new private Session only on cache misses, under the lock.
    Eviction drops cache ownership without closing a client a caller may still be
    using; idle evictees are reclaimed by the SDK/pool GC. Shutdown closes all
    still-live clients after workers have drained, and is also registered at exit.
    """
    with _lock:
        policy = "preflight" if preflight else "write" if non_idempotent else "read"
        key = (factory, region, policy, _configuration_key())
        if key in _clients:
            _clients.move_to_end(key)
            return _clients[key]
        config = (Config(connect_timeout=2, read_timeout=3,
                         retries={"mode": "standard", "total_max_attempts": 1})
                  if preflight else Config(retries={"mode": "standard", "total_max_attempts": 1})
                  if non_idempotent else Config(retries={"mode": "standard", "max_attempts": 4}))
        creator = boto3.session.Session().client if factory is _DEFAULT_FACTORY else factory
        client = creator("s3", region_name=region, config=config)
        configure_upload_client(client)
        _clients[key] = client
        _live_clients.add(client)
        if len(_clients) > _MAX_CLIENTS:
            _clients.popitem(last=False)
        return client


def shutdown_s3_clients() -> None:
    """Close this process's clients only; call after all users have drained."""
    with _lock:
        clients = list(_live_clients)
        _clients.clear()
        _live_clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            # Never log provider details or credentials, especially at exit.
            pass


atexit.register(shutdown_s3_clients)
