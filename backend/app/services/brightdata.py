"""
Sync httpx wrapper for Bright Data's dataset scrape API.

The scrape API is async at the vendor side — the call sequence is:
  1. POST /datasets/v3/scrape?dataset_id=...&type=discover_new&discover_by=keyword
     Body: {"input": [<one or more search records>]}
     Response: {"snapshot_id": "..."}
  2. GET /datasets/v3/snapshot/{snapshot_id}
     Polled until status="ready"; then returns the scraped rows.

This module exposes a `submit_and_wait` helper that hides the polling and
returns the parsed rows. Caller is responsible for converting those rows into
candidate documents (the dataset shape varies — LinkedIn jobs vs LinkedIn
posts have different fields).

Defaults align with the LinkedIn job listings dataset (gd_lpfll7v5hcqtkxl6l)
because that's the dataset the engine was set up to query first. Override
`dataset_id` / `discover_by` per-call to target other Bright Data datasets.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_BASE_URL = "https://api.brightdata.com"
_TIMEOUT = 60.0
# Bright Data tolerates parallel snapshots, but we cap to keep credit usage
# predictable + so we don't trip account-level rate limits.
_MAX_CONCURRENCY = 3
_concurrency = threading.Semaphore(_MAX_CONCURRENCY)


class BrightDataError(RuntimeError):
    pass


class BrightDataNotConfigured(BrightDataError):
    pass


class BrightDataTimeout(BrightDataError):
    """Snapshot did not reach `ready` within the polling budget."""


@dataclass(frozen=True)
class BrightDataSnapshot:
    snapshot_id: str
    dataset_id: str
    status: str
    rows: list[dict[str, Any]]


def _client() -> httpx.Client:
    if not settings.brightdata_api_key:
        raise BrightDataNotConfigured(
            "BRIGHTDATA_API_KEY is not set. Add it to .env to enable Bright Data discovery."
        )
    return httpx.Client(
        base_url=_BASE_URL,
        headers={
            "Authorization": f"Bearer {settings.brightdata_api_key}",
            "Content-Type": "application/json",
        },
        timeout=_TIMEOUT,
    )


def submit_search_request(
    inputs: list[dict[str, Any]],
    *,
    dataset_id: str | None = None,
    discover_by: str = "keyword",
    notify: bool = False,
    include_errors: bool = True,
) -> str:
    """Kick off a Bright Data scrape. Returns the snapshot_id to poll.

    `inputs` is the list of search records — for the LinkedIn jobs dataset
    each record looks like:
        {"location":"paris","keyword":"product manager","country":"FR",
         "time_range":"Past month","job_type":"Full-time",
         "experience_level":"Internship","remote":"On-site",
         "company":"","location_radius":""}

    Other dataset_ids accept different fields — refer to the Bright Data
    dataset docs for the schema.
    """
    if not inputs:
        raise BrightDataError("submit_search_request: inputs must be non-empty")
    ds = dataset_id or settings.brightdata_jobs_dataset_id
    if not ds:
        raise BrightDataError("submit_search_request: dataset_id is required")

    params = {
        "dataset_id": ds,
        "notify": "true" if notify else "false",
        "include_errors": "true" if include_errors else "false",
        "type": "discover_new",
        "discover_by": discover_by,
    }
    body = {"input": inputs}

    with _concurrency:
        try:
            with _client() as client:
                resp = client.post("/datasets/v3/scrape", params=params, json=body)
        except httpx.RequestError as err:
            raise BrightDataError(f"brightdata transport error: {err}") from err

    if resp.status_code == 401:
        raise BrightDataError(f"brightdata 401 auth failure: {resp.text[:300]}")
    if resp.status_code == 402:
        raise BrightDataError(f"brightdata 402 payment required: {resp.text[:300]}")
    if resp.status_code == 429:
        raise BrightDataError(f"brightdata 429 rate limit: {resp.text[:300]}")
    if resp.status_code >= 400:
        raise BrightDataError(
            f"brightdata {resp.status_code}: {resp.text[:300]}"
        )

    payload = resp.json()
    snapshot_id = payload.get("snapshot_id") or payload.get("id")
    if not snapshot_id:
        raise BrightDataError(
            f"brightdata: response missing snapshot_id (got keys={list(payload)})"
        )
    log.info(
        "brightdata: submitted scrape dataset=%s discover_by=%s inputs=%d snapshot=%s",
        ds, discover_by, len(inputs), snapshot_id,
    )
    return snapshot_id


def fetch_snapshot(snapshot_id: str) -> BrightDataSnapshot:
    """One non-blocking peek at the snapshot. Status is one of:
    `running`, `building`, `ready`, `failed`. Rows are populated only on
    `ready`."""
    if not snapshot_id:
        raise BrightDataError("fetch_snapshot: snapshot_id is required")

    with _concurrency:
        try:
            with _client() as client:
                resp = client.get(f"/datasets/v3/snapshot/{snapshot_id}")
        except httpx.RequestError as err:
            raise BrightDataError(f"brightdata transport error: {err}") from err

    if resp.status_code == 404:
        raise BrightDataError(f"brightdata: snapshot {snapshot_id} not found")
    if resp.status_code >= 400:
        raise BrightDataError(
            f"brightdata snapshot {resp.status_code}: {resp.text[:300]}"
        )

    payload = resp.json()
    # The snapshot endpoint returns a JSON array on success (the rows
    # themselves) or an object with a `status` field while the snapshot is
    # still building. We normalise both shapes.
    if isinstance(payload, list):
        return BrightDataSnapshot(
            snapshot_id=snapshot_id,
            dataset_id="",
            status="ready",
            rows=payload,
        )
    status = (payload.get("status") or "running").lower()
    rows = payload.get("data") or payload.get("rows") or []
    return BrightDataSnapshot(
        snapshot_id=snapshot_id,
        dataset_id=payload.get("dataset_id") or "",
        status=status,
        rows=list(rows),
    )


def submit_and_wait(
    inputs: list[dict[str, Any]],
    *,
    dataset_id: str | None = None,
    discover_by: str = "keyword",
    timeout_s: int | None = None,
    interval_s: int | None = None,
) -> BrightDataSnapshot:
    """End-to-end: submit a scrape, poll until ready (or timeout), return
    the parsed snapshot. Raises `BrightDataTimeout` if the snapshot never
    reaches `ready` within `timeout_s`."""
    snapshot_id = submit_search_request(
        inputs, dataset_id=dataset_id, discover_by=discover_by
    )
    deadline = time.monotonic() + (timeout_s or settings.brightdata_poll_timeout_s)
    interval = max(2, interval_s or settings.brightdata_poll_interval_s)

    while True:
        snap = fetch_snapshot(snapshot_id)
        if snap.status == "ready":
            log.info(
                "brightdata: snapshot %s ready (%d rows)", snapshot_id, len(snap.rows)
            )
            return snap
        if snap.status == "failed":
            raise BrightDataError(
                f"brightdata snapshot {snapshot_id} failed (status={snap.status})"
            )
        if time.monotonic() >= deadline:
            raise BrightDataTimeout(
                f"brightdata snapshot {snapshot_id} not ready after "
                f"{timeout_s or settings.brightdata_poll_timeout_s}s "
                f"(last_status={snap.status})"
            )
        log.debug(
            "brightdata: snapshot %s status=%s, sleeping %ds", snapshot_id, snap.status, interval,
        )
        time.sleep(interval)


def search_jobs_by_keyword(
    queries: Iterable[dict[str, Any]],
    *,
    timeout_s: int | None = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper for the LinkedIn jobs dataset.

    `queries` is an iterable of search records (each one a dict matching the
    jobs dataset schema — see `submit_search_request` docstring). Returns the
    raw rows from the snapshot once it's ready.
    """
    inputs = list(queries)
    if not inputs:
        return []
    snap = submit_and_wait(
        inputs,
        dataset_id=settings.brightdata_jobs_dataset_id,
        discover_by="keyword",
        timeout_s=timeout_s,
    )
    return snap.rows
