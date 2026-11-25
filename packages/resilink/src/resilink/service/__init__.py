"""Within-system service models (slot ``service``, ADR-0008): who is served inside one system, and how well."""

from resilink.service.connectivity import ConnectivityService, served

__all__ = ["ConnectivityService", "served"]
