"""Agent-side marker for the unified Session Hub package.

The package intentionally registers no agent tools or hooks. Its separately
gated dashboard router and Desktop UI are discovered by Hermes 0.20.5.
"""


def register(ctx):
    """Register no mutable agent capabilities."""
    return None
