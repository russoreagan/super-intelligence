"""Multi-tenant gateway (JupyterHub-style Hub): auth + key vault + spawn + proxy."""

from brain.gateway.server import build_gateway_app

__all__ = ["build_gateway_app"]
