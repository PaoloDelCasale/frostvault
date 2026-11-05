"""One safe Owner destination shared by SMTP and the in-app inbox."""
from urllib.parse import urlencode, urlsplit
from typing import Any


def notification_path(vault_id: int | None, params: dict[str, Any]) -> str:
    if not vault_id:
        return "/general"
    query: dict[str, str] = {"vault_id": str(vault_id)}
    path = params.get("path")
    if isinstance(path, str) and path:
        query.update(directory=path.rpartition("/")[0], file=path)
    else:
        query["settings"] = "1"
    return "/?" + urlencode(query)


def notification_url(base: str, vault_id: int | None, params: dict[str, Any]) -> str | None:
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    return base.rstrip("/") + notification_path(vault_id, params)
