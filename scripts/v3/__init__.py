"""Colixo SEO / Market Intelligence Agent V3 foundation.

Phase 1 is deliberately offline, read-only, and proposal-only.  Source
adapters consume local fixtures; no adapter performs network I/O.
"""

from .agent import AgentResult, MarketIntelligenceAgent

__all__ = ["AgentResult", "MarketIntelligenceAgent"]
