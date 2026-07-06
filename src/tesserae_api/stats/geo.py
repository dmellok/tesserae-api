"""GeoIP lookup against a MaxMind GeoLite2 database.

The IP address is used only to resolve a coarse country + region and is never
stored or logged. If the database is missing or the IP does not resolve, the
lookup returns (None, None) and the request is still served and counted.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock

import geoip2.database
from geoip2.errors import AddressNotFoundError

_reader: geoip2.database.Reader | None = None
_reader_path: Path | None = None
_lock = Lock()


def _get_reader(db_path: Path) -> geoip2.database.Reader | None:
    global _reader, _reader_path
    with _lock:
        if _reader is not None and _reader_path == db_path:
            return _reader
        if not db_path.exists():
            return None
        _reader = geoip2.database.Reader(str(db_path))
        _reader_path = db_path
        return _reader


def lookup(ip: str | None, db_path: Path) -> tuple[str | None, str | None]:
    """Return (country_iso, region_name) for an IP. (None, None) on any failure."""
    if not ip:
        return None, None
    reader = _get_reader(db_path)
    if reader is None:
        return None, None
    try:
        response = reader.city(ip)
    except (AddressNotFoundError, ValueError):
        return None, None
    country = response.country.iso_code
    region = response.subdivisions.most_specific.name if response.subdivisions else None
    return country, region


def close() -> None:
    """Release the reader (used at application shutdown and in tests)."""
    global _reader, _reader_path
    with _lock:
        if _reader is not None:
            _reader.close()
        _reader = None
        _reader_path = None
