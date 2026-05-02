"""
Thin Deribit public-API client. No auth needed for any of these endpoints.
"""

from __future__ import annotations

import time
import urllib.request
import json
import ssl
from typing import Any

DERIBIT = "https://www.deribit.com/api/v2"
_CTX = ssl.create_default_context()
_HEADERS = {"User-Agent": "oi-collector/1.0", "Accept": "application/json"}


def _get(path: str, params: dict | None = None, retries: int = 3,
         backoff: float = 1.5) -> Any:
    """GET with retry. Raises on final failure."""
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{DERIBIT}{path}{qs}"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
                data = json.loads(resp.read())
            if "result" in data:
                return data["result"]
            if "error" in data:
                raise RuntimeError(f"Deribit error: {data['error']}")
            return data
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
    raise RuntimeError(f"Deribit GET {path} failed after {retries} retries: {last_err}")


def book_summary(currency: str, kind: str = "option") -> list[dict]:
    """All instruments of a kind for a currency. Includes OI, mark, IV."""
    return _get("/public/get_book_summary_by_currency",
                {"currency": currency, "kind": kind})


def get_index_price(index_name: str) -> float:
    """Index price like btc_usd or eth_usd."""
    res = _get("/public/get_index_price", {"index_name": index_name})
    return float(res["index_price"])


def get_ticker(instrument_name: str) -> dict:
    """Full ticker incl. greeks for a single instrument."""
    return _get("/public/ticker", {"instrument_name": instrument_name})


def get_instruments(currency: str, kind: str = "option",
                    expired: bool = False) -> list[dict]:
    """List of instruments with strike, expiry, etc."""
    return _get("/public/get_instruments",
                {"currency": currency, "kind": kind,
                 "expired": str(expired).lower()})
