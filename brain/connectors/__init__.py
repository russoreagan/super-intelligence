"""Connector catalogue + OAuth client for third-party MCP servers.

The registry itself (register / remove / load) lives in
brain/clusters/cma_executor.py; this package holds what is independent of the
executor: the list of known remote MCP servers (catalog.py) and the OAuth 2.1
client that turns a "Connect" click into stored tokens (oauth.py).
"""
