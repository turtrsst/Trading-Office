"""Trading Office — an Expert-Advisor style trading engine for IDX equities and crypto futures.

Pipeline (see docs/STRATEGY_LOGIC.md):

    [ Market Filter ] -> [ Signal Generation ] -> [ Risk Validation ] -> [ Order Execution ]

The package is import-safe without third-party dependencies: pandas / numpy / ccxt are
optional and only required by the modules that actually need them.
"""

__version__ = "1.0.0"

__all__ = [
    "config",
    "indicators",
    "market_filter",
    "signals",
    "risk",
    "management",
    "engine",
    "state",
]
