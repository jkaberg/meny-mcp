"""Weekly menu planning against Mealie.

This is a second, optional toolset on the same server. It registers only when
``MEALIE_URL`` and ``MEALIE_TOKEN`` are set, so a server configured purely for
groceries advertises purely grocery tools.

It lives here rather than in a skill's scripts because the Claude Desktop
sandbox cannot reach a self-hosted Mealie on the local network. The MCP server
already runs locally, so it can.
"""
