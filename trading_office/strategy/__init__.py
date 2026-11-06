"""Strategy layer: the shared four-gate scan used by the API, CLI and screener."""

from .runner import ScanResult, scan_symbol  # noqa: F401

__all__ = ["ScanResult", "scan_symbol"]
