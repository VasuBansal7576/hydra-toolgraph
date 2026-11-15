"""Minimal HydraDB HTTP client for the local graph-node."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class HydraError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HydraClient:
    def __init__(self, base_url: str, token: str, namespace: str = "default", graph: str = "default") -> None:
        self.url = f"{base_url.rstrip('/')}/v1/graphs/{graph}/query"
        self.token = token
        self.namespace = namespace

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        body: dict = {"cell_id": "cell-0", "query": statement}
        if parameters is not None:
            body["parameters"] = parameters
        rows: list = []
        columns: list = []
        bookmark = None
        for _page in range(20):
            page = self._post(body)
            columns = page.get("columns") or columns
            rows.extend(page.get("rows") or [])
            bookmark = page.get("bookmark", bookmark)
            cursor = page.get("next_cursor")
            if not cursor:
                break
            body = {"cursor": cursor}
        return {"columns": columns, "rows": rows, "bookmark": bookmark}

    def _post(self, body: dict) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Graph-Namespace": self.namespace,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            raise HydraError(f"HTTP {exc.code}: {detail}") from exc


def unwrap(cell: object) -> object:
    if isinstance(cell, dict) and "value" in cell and "type" in cell:
        return unwrap(cell["value"])
    if isinstance(cell, dict) and set(cell.keys()) == {"String"}:
        return cell["String"]
    if isinstance(cell, dict) and set(cell.keys()) == {"Float"}:
        return cell["Float"]
    if isinstance(cell, dict) and set(cell.keys()) == {"Integer"}:
        return cell["Integer"]
    if isinstance(cell, dict) and set(cell.keys()) == {"Bool"}:
        return cell["Bool"]
    if isinstance(cell, dict) and set(cell.keys()) == {"Boolean"}:
        return cell["Boolean"]
    return cell


class RecordingHydraClient:
    """Wraps HydraClient and counts real store requests (for the two-request test)."""

    def __init__(self, inner: HydraClient) -> None:
        self.inner = inner
        self.calls: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def reset(self) -> None:
        self.calls.clear()

    def query(self, statement: str, parameters: dict | None = None) -> dict:
        self.calls.append(statement)
        return self.inner.query(statement, parameters)


def as_float(cell: object) -> float:
    value = unwrap(cell)
    return float(value)  # type: ignore[arg-type]


def as_str(cell: object) -> str | None:
    value = unwrap(cell)
    if isinstance(value, str):
        return value
    return None
