"""HTTP surface: REST for the web terminal and agents, plus an MCP server over stdio/HTTP."""

from .server import app, create_app  # noqa: F401

__all__ = ["app", "create_app"]
