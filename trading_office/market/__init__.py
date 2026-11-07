"""Market data adapters: IDX via the ``idx`` CLI, crypto via ``ccxt``, CFDs via Exness,
synthetic demo data for all three."""

from .crypto_client import CryptoClient, CryptoUnavailable  # noqa: F401
from .fx_client import FxClient, FxUnavailable  # noqa: F401
from .idx_client import IdxClient, IdxUnavailable  # noqa: F401

__all__ = [
    "CryptoClient", "CryptoUnavailable",
    "FxClient", "FxUnavailable",
    "IdxClient", "IdxUnavailable",
]
