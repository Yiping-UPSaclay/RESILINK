"""Built-in adapters (importing this package registers them in ``resilink.etl.staging.ADAPTERS``)."""

from resilink.etl.adapters import admin_express, filosofi  # noqa: F401  (registration)
from resilink.etl.staging import ADAPTERS

__all__ = ["ADAPTERS"]
