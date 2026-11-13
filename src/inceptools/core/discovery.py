"""Find providers and handlers by what they declared.

The pattern this exists for: a decorator marks methods (``@job``, ``@consumes``,
``@subscribes``), and something at boot has to find every marked method across
every module and wire it up. Without discovery, each module has to register
its own, and the one that forgets is the one whose jobs never run.

    for found in discovery.handlers_with("inceptools:job"):
        scheduler.add(Job(run=found.method, **found.metadata))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from inceptools.core.metadata import all_metadata, get_metadata, has_metadata

__all__ = ["DiscoveredHandler", "DiscoveredProvider", "DiscoveryService"]


@dataclass(frozen=True, slots=True)
class DiscoveredProvider:
    """One constructed provider, and where it came from."""

    instance: Any
    module: str

    @property
    def type(self) -> type:
        return type(self.instance)

    def metadata(self, key: str, default: Any = None) -> Any:
        return get_metadata(key, type(self.instance), default)


@dataclass(frozen=True, slots=True)
class DiscoveredHandler:
    """One marked method on a provider or controller."""

    instance: Any
    name: str
    module: str
    metadata: dict[str, Any]

    @property
    def method(self) -> Any:
        """The bound method, ready to call."""
        return getattr(self.instance, self.name)


class DiscoveryService:
    """Walks the application's built modules. Injectable."""

    def __init__(self, application: Any) -> None:
        self._app = application

    def providers(self) -> list[DiscoveredProvider]:
        """Every instance every module container built."""
        found: list[DiscoveredProvider] = []
        seen: set[int] = set()
        for item in getattr(self._app, "_built", []):
            for instance in item.container.instances():
                if id(instance) in seen:
                    continue
                seen.add(id(instance))
                found.append(DiscoveredProvider(instance=instance, module=item.qualified_name))
        for instance in self._app.container.instances():
            if id(instance) not in seen:
                seen.add(id(instance))
                found.append(DiscoveredProvider(instance=instance, module=""))
        return found

    def controllers(self) -> list[DiscoveredProvider]:
        return [
            DiscoveredProvider(instance=controller, module=item.qualified_name)
            for item in getattr(self._app, "_built", [])
            for controller in item.controllers
        ]

    def providers_with(self, key: str) -> list[DiscoveredProvider]:
        """Providers whose **class** carries *key*."""
        return [p for p in self.providers() if has_metadata(key, type(p.instance))]

    def handlers_with(self, key: str) -> list[DiscoveredHandler]:
        """Every method carrying *key*, across providers and controllers."""
        found: list[DiscoveredHandler] = []
        for holder in (*self.providers(), *self.controllers()):
            cls = type(holder.instance)
            for name in dir(cls):
                if name.startswith("__"):
                    continue
                member = getattr(cls, name, None)
                if member is None or not callable(member):
                    continue
                if not has_metadata(key, member):
                    continue
                found.append(
                    DiscoveredHandler(
                        instance=holder.instance,
                        name=name,
                        module=holder.module,
                        metadata=all_metadata(member),
                    )
                )
        return found
