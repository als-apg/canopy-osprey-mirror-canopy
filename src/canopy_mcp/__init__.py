"""canopy_mcp — the panel-builder MCP server.

A separate top-level package in the canopy wheel (installed with the ``mcp``
extra). It talks to the canopy backend over HTTP only; import-linter forbids
it from reaching into canopy modules other than the shared kernel and the formula-engine
gateway, and forbids canopy from importing it at all.
"""
