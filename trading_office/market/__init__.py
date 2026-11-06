"""Market data adapters: IDX via the ``idx`` CLI, crypto via ``ccxt``, synthetic demo data."""

from .crypto_client import CryptoClient, CryptoUnavailable  # noqa: F401
from .idx_client import IdxClient, IdxUnavailable  # noqa: F401

__all__ = ["CryptoClient", "CryptoUnavailable", "IdxClient", "IdxUnavailable"]
