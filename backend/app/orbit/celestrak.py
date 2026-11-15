import json
from typing import Any, Optional
import httpx

from app.config import CELESTRAK_BASE_URL, CELESTRAK_TIMEOUT_SECONDS
from app.orbit.exceptions import CelesTrakError, CelesTrakTimeoutError


class CelesTrakClient:
    """HTTP client adapter for CelesTrak public General Perturbations (GP) API."""

    def __init__(
        self,
        base_url: str = CELESTRAK_BASE_URL,
        timeout: float = CELESTRAK_TIMEOUT_SECONDS,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self._external_client = client

    async def fetch_gp_by_catnr(self, catnr: int) -> list[dict[str, Any]]:
        """Fetch GP JSON data by NORAD Catalog Number."""
        return await self._request({"CATNR": str(catnr), "FORMAT": "JSON"})

    async def fetch_gp_by_group(self, group: str) -> list[dict[str, Any]]:
        """Fetch GP JSON data by group name (e.g. 'stations', 'visual', 'active')."""
        return await self._request({"GROUP": group, "FORMAT": "JSON"})

    async def _request(self, params: dict[str, str]) -> list[dict[str, Any]]:
        headers = {"User-Agent": "SatView/0.1.0 (+https://github.com/vVIBHORj/SatView)"}

        if self._external_client:
            return await self._do_fetch(self._external_client, params, headers)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await self._do_fetch(client, params, headers)

    async def _do_fetch(
        self, client: httpx.AsyncClient, params: dict[str, str], headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        try:
            response = await client.get(self.base_url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise CelesTrakTimeoutError(
                f"Request to CelesTrak timed out after {self.timeout}s"
            ) from exc
        except httpx.RequestError as exc:
            raise CelesTrakError(f"CelesTrak network error: {exc}") from exc

        # 404 Not Found indicates object does not exist in catalog
        if response.status_code == 404 or "No GP data found" in response.text:
            return []

        if response.status_code != 200:
            raise CelesTrakError(
                f"CelesTrak returned unexpected HTTP status {response.status_code}: {response.text[:200]}"
            )

        text = response.text.strip()
        # CelesTrak sometimes returns 200 with error text (e.g. 'Invalid query: ...')
        if text.startswith("Invalid query:") or "not found" in text.lower() and not text.startswith("["):
            raise CelesTrakError(f"CelesTrak rejected query: {text[:200]}")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CelesTrakError(
                f"Failed to parse CelesTrak JSON response: {text[:150]}"
            ) from exc

        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [data]
        else:
            raise CelesTrakError(f"Unexpected JSON data format: expected list, got {type(data)}")
