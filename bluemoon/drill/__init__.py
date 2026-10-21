"""The DRILL: Data Resolution through Incremental List Liquidation.

- engine:  contact levels, stages, check-offs, people, contacts, outreach, work queues, the DRILL board.
- links:   pre-filled research links from config/sources.yaml (and the owner-name -> person parser).
- credits: the Crexi owner-contact credit ledger and the monthly spend-them-all plan.
- stats:   hit rates by stage and owner type, tips, and the CRM handoff rows.

Import the submodules directly (`from bluemoon.drill import engine`); this package does not import them
eagerly, so `bluemoon.ingest` can import the engine without pulling in everything else.
"""
from __future__ import annotations

__all__ = ["credits", "engine", "links", "stats"]
