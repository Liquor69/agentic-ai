"""
domains/__init__.py

Domain pack loader.

Usage:
    from domains import load_domain_pack
    pack = load_domain_pack("customer_ops")   # or settings.domain_pack

Adding a new domain:
    1. Create domains/<name>.py with a PACK: DomainPack dict.
    2. Create tools/<name>.py with @register_tool functions.
    3. Set DOMAIN_PACK=<name> in .env.

No other changes required.
"""

from __future__ import annotations

import importlib

from domains.base import DomainPack

# Known domain names → module paths.
# Extend this dict when adding a new domain pack.
_DOMAIN_REGISTRY: dict[str, str] = {
    "customer_ops": "domains.customer_ops",
    "it_helpdesk":  "domains.it_helpdesk",
    "appointments": "domains.appointments",
}


def load_domain_pack(name: str) -> DomainPack:
    """
    Load and return the domain pack for *name*.

    Parameters
    ----------
    name : str
        Domain identifier (matches the DOMAIN_PACK config key and the filename
        under domains/, e.g. "customer_ops" or "it_helpdesk").

    Returns
    -------
    DomainPack
        The domain configuration dict.

    Raises
    ------
    ValueError
        If *name* is not registered in _DOMAIN_REGISTRY.
    """
    module_path = _DOMAIN_REGISTRY.get(name)
    if module_path is None:
        available = ", ".join(sorted(_DOMAIN_REGISTRY))
        raise ValueError(
            f"Unknown domain pack '{name}'. "
            f"Available: {available}. "
            f"To add a new domain, create domains/{name}.py and register it in "
            "domains/__init__._DOMAIN_REGISTRY."
        )
    module = importlib.import_module(module_path)
    return module.PACK


def list_domain_packs() -> list[str]:
    """Return the names of all registered domain packs."""
    return sorted(_DOMAIN_REGISTRY)
