"""Built-in adapters (importing this package registers them in ``resilink.etl.staging.ADAPTERS``)."""

from resilink.etl.adapters import (  # noqa: F401  (registration)
    admin_express,
    arcep,
    enedis,
    filosofi,
    gas,
    osm_power,
    osm_telecom,
    rte,
)
from resilink.etl.staging import ADAPTERS

__all__ = ["ADAPTERS"]
