"""HTTP client communicating with the Simulator over HTTP."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("controller.client")


class SimulatorClientError(Exception):
    def __init__(self, status_code: int, code: str, detail: Any):
        super().__init__(f"Simulator API Error {status_code} [{code}]: {detail}")
        self.status_code = status_code
        self.code = code
        self.detail = detail


class SimulatorClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8700", timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        retries = 3
        backoff = 0.5
        for attempt in range(retries):
            try:
                res = self._client.request(method, path, **kwargs)
                if res.status_code >= 400:
                    err_code = "http_error"
                    err_detail = res.text
                    try:
                        data = res.json()
                        if isinstance(data, dict):
                            err_code = data.get("error", err_code)
                            err_detail = data.get("detail", err_detail)
                    except Exception:
                        pass
                    raise SimulatorClientError(res.status_code, err_code, err_detail)
                return res
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                if attempt == retries - 1:
                    logger.error(f"Network error calling {method} {path}: {e}")
                    raise
                time.sleep(backoff)
                backoff *= 2

    # --- Simulator State & Clock ---
    def get_state(self) -> dict[str, Any]:
        return self._request("GET", "/sim/state").json()

    def advance(self, minutes: int = 60) -> dict[str, Any]:
        return self._request("POST", "/sim/advance", json={"minutes": minutes}).json()

    # --- Chunks & Events ---
    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict[str, Any]]:
        return self._request("GET", "/events/chunks", params={"kind": kind, "after": after}).json()

    def get_chunk_stream(self, chunk_no: int):
        # Stream response text
        res = self._request("GET", f"/events/chunks/{chunk_no}")
        return res.iter_lines()

    # --- Workflows & Config ---
    def get_workflows(self) -> list[dict[str, Any]]:
        return self._request("GET", "/workflows").json()

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}").json()

    def get_workflow_config(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}/config").json()

    def auto_apply_config(self, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/workflows/{workflow_id}/config", json=body).json()

    # --- Approvals ---
    def request_approval(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/approvals", json=body).json()

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        return self._request("GET", f"/approvals/{approval_id}").json()

    def list_approvals(self, workflow_id: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]:
        params = {}
        if workflow_id:
            params["workflow_id"] = workflow_id
        if status:
            params["status"] = status
        return self._request("GET", "/approvals", params=params).json()

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{workflow_id}/apply", json={"approval_id": approval_id}).json()

    # --- LLM & Validation ---
    def suggest_fixes(self, workflow_id: str, problem: str) -> dict[str, Any]:
        return self._request("POST", "/llm/suggest-fixes", json={"workflow_id": workflow_id, "problem": problem}).json()

    def run_validation(self, workflow_id: str, prompt_version: str, n: int = 400) -> dict[str, Any]:
        return self._request("POST", "/validation/runs", json={
            "workflow_id": workflow_id,
            "prompt_version": prompt_version,
            "n": n,
        }).json()

