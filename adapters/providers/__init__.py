"""Provider directory adapters.

Each subpackage implements :class:`~adapters.base.SiteAdapter` for one health
system's public "Find a Doctor" website. Add a new one by writing an adapter
in ``adapters/providers/<system>/`` and registering it in
``adapters/__init__.py``'s ``ADAPTERS``.
"""

from __future__ import annotations
