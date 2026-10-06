"""HTTP access to one XL device.

Every request is a GET; nothing here can change device configuration.

Three implementations share one method, get_json(path, params):

  XLClient       live HTTP, with a hard per-device request ceiling
  RecordingClient  wraps a live client and saves every response to disk
  ReplayClient   serves those saved responses back, no device needed

Recorded responses are the fixture format for the tests, and let a sync
be re-run offline against exactly what a device said.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import requests


class Client(Protocol):
    host: str

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any: ...


class DeviceHTTPError(RuntimeError):
    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"HTTP {status} from {path}: {body[:200]}")
        self.status = status
        self.path = path


class XLClient:
    """Live device. Thread-safe; one instance per device."""

    def __init__(self, host: str, *, timeout: float = 5.0, max_rps: float = 8.0) -> None:
        self.host = host
        self.timeout = timeout
        # The real-time API tolerates 10 requests/s. Stay under it with
        # margin, enforced here so no caller can exceed it by accident.
        self._min_gap = 1.0 / max_rps if max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self.requests_made = 0

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last + self._min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests_made += 1

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        self._throttle()
        url = f"http://{self.host}/{path.lstrip('/')}"
        resp = self._session.get(url, params=params, timeout=self.timeout)
        if resp.status_code >= 400:
            raise DeviceHTTPError(resp.status_code, path, resp.text)
        return resp.json()

    def close(self) -> None:
        self._session.close()


# --------------------------------------------------------------- fixtures


def fixture_name(path: str, params: dict[str, str] | None) -> str:
    """Stable, short, Windows-safe filename for one request.

    The fields list for a closed-event request can run to 150 names, so the
    params are hashed rather than spelled out.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/")).strip("_")
    if params:
        canon = json.dumps(params, sort_keys=True)
        slug += "__" + hashlib.sha1(canon.encode()).hexdigest()[:10]
    return slug + ".json"


class RecordingClient:
    def __init__(self, inner: Client, directory: str | Path) -> None:
        self.inner = inner
        self.host = inner.host
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        status, body = 200, None
        try:
            body = self.inner.get_json(path, params)
            return body
        except DeviceHTTPError as exc:
            status = exc.status
            body = {"error": str(exc)}
            raise
        finally:
            record = {
                "request": {"path": path, "params": params or {}},
                "status": status,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "response": body,
            }
            (self.dir / fixture_name(path, params)).write_text(
                json.dumps(record, indent=1, default=str), encoding="utf-8"
            )


class ReplayClient:
    def __init__(self, directory: str | Path, host: str = "replay") -> None:
        self.dir = Path(directory)
        self.host = host
        if not self.dir.is_dir():
            raise FileNotFoundError(f"no recorded payloads in {self.dir}")

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        f = self.dir / fixture_name(path, params)
        if not f.exists():
            raise DeviceHTTPError(404, path, f"not recorded ({f.name})")
        record = json.loads(f.read_text(encoding="utf-8"))
        if record.get("status", 200) >= 400:
            raise DeviceHTTPError(record["status"], path, str(record.get("response")))
        return record["response"]
