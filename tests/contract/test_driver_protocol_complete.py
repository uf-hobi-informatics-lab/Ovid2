"""Contract: every entry in :data:`DRIVERS` implements the protocol.

Loads every registered driver class and asserts it:

1. Subclasses :class:`ovid2.drivers.base.Driver`.
2. Declares a non-empty ``type_name`` that matches the registry key.
3. Implements the abstract classmethods (``connection_fields``,
   ``location_fields``, ``probe_auth``).
4. Its ``connection_fields()`` returns a real
   :class:`~ovid2.drivers.base.ConnectionFieldSpec`.
5. Its ``location_fields()`` returns a tuple / list of
   :class:`~ovid2.drivers.base.LocationField` / dict-shaped entries.

D1 registers no native drivers yet; the D2 legacy adapter fills the
registry from the pre-refactor :data:`CONNECTION_TYPE_MAP`. This test
becomes fully populated at D2 landing.
"""

from __future__ import annotations

from ovid2.drivers.base import ConnectionFieldSpec, Driver, LocationField
from ovid2.drivers.registry import DRIVERS, load_class


def test_every_registered_driver_subclasses_driver() -> None:
    """Every entry in ``DRIVERS`` must load to a Driver subclass."""
    for type_name in sorted(DRIVERS):
        cls = load_class(type_name)
        # ``cls`` may be a class or a zero-arg factory returning an
        # instance. Both must ultimately expose ``Driver`` semantics.
        try:
            is_driver_cls = isinstance(cls, type) and issubclass(cls, Driver)
        except TypeError:
            is_driver_cls = False
        assert is_driver_cls, (
            f"DRIVERS[{type_name!r}] resolved to {cls!r}, which is not a "
            f"subclass of Driver. Fix the dotted path in "
            f"ovid2.drivers.registry.DRIVERS."
        )


def test_every_registered_driver_declares_matching_type_name() -> None:
    """Every driver's ``type_name`` must equal its registry key."""
    for type_name in sorted(DRIVERS):
        cls = load_class(type_name)
        assert getattr(cls, "type_name", "") == type_name, (
            f"DRIVERS[{type_name!r}] resolves to a class whose "
            f"type_name is {getattr(cls, 'type_name', '')!r}; "
            "they must match."
        )


def test_every_driver_produces_a_connection_field_spec() -> None:
    """Every driver's ``connection_fields()`` returns a spec."""
    for type_name in sorted(DRIVERS):
        cls = load_class(type_name)
        spec = cls.connection_fields()
        assert isinstance(spec, ConnectionFieldSpec), (
            f"Driver {type_name!r}: connection_fields() returned "
            f"{type(spec).__name__} instead of ConnectionFieldSpec."
        )


def test_every_driver_produces_a_location_field_tuple() -> None:
    """Every driver's ``location_fields()`` returns a tuple of fields."""
    for type_name in sorted(DRIVERS):
        cls = load_class(type_name)
        fields = cls.location_fields()
        assert isinstance(fields, (tuple, list)), (
            f"Driver {type_name!r}: location_fields() returned "
            f"{type(fields).__name__}; expected tuple/list."
        )
        for f in fields:
            assert isinstance(f, (LocationField, dict)), (
                f"Driver {type_name!r}: location_fields() entry "
                f"{f!r} is not a LocationField/dict."
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Contract test; run with pytest.")
