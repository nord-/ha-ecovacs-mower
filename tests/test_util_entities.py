"""get_supported_entities requires HA imports and therefore runs in CI."""

from tests import requires_ha

pytestmark = requires_ha


def test_only_devices_with_the_capability_get_an_entity() -> None:
    """Descriptions whose capability_fn yields None must not become entities.

    It is this filtering that spares a lawn mower from vacuum entities without us
    having to enumerate them.
    """
    from dataclasses import dataclass
    from unittest.mock import Mock

    from custom_components.ecovacs_mower.entity import (
        EcovacsCapabilityEntityDescription,
    )
    from custom_components.ecovacs_mower.util import get_supported_entities

    @dataclass(kw_only=True, frozen=True)
    class _Description(EcovacsCapabilityEntityDescription):
        pass

    class _Entity:
        """Stand-in for entity_class.

        Cannot be ``Mock`` itself: ``Mock.__init__`` reads its first positional
        argument as ``spec``, and get_supported_entities calls
        ``entity_class(device, capability, description)`` positionally. With a
        ``device`` that is already a ``Mock`` that collides with
        ``InvalidSpecError: Cannot spec a Mock object``.
        """

        def __init__(
            self, device: object, capability: object, description: object
        ) -> None:
            self.device = device
            self.capability = capability
            self.description = description

    has_it = _Description(key="has_it", capability_fn=lambda caps: caps.battery)
    lacks_it = _Description(key="lacks_it", capability_fn=lambda caps: caps.water)

    device = Mock()
    device.capabilities.battery = object()
    device.capabilities.water = None
    controller = Mock()
    controller.devices = [device]

    created = get_supported_entities(controller, _Entity, (has_it, lacks_it))

    assert len(created) == 1
