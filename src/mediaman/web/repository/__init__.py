"""Web-layer repository modules.

These modules encapsulate SQL operations that are specific to the web layer
(not shared with the scanner). Tables accessed here: subscribers, suggestions,
media_items / scheduled_actions / audit_log (via the library_api repository),
and delete_intents.
"""

from __future__ import annotations
