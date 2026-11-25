"""Built-in system packs (ADR-0008 §2): electricity, gas, mobile telecommunications and critical facilities.

They are registered through the core's own ``resilink.systems`` entry points, exactly like a pack from a separate
package (for example a water system contributed by a thesis). Each pack is plain data in a YAML file here.
"""

from pathlib import Path

from resilink.plugin_api import SystemPack

_HERE = Path(__file__).parent

ELEC = SystemPack.from_yaml(_HERE / "elec.yaml")
FACILITY = SystemPack.from_yaml(_HERE / "facility.yaml")
GAS = SystemPack.from_yaml(_HERE / "gas.yaml")
TELECOM = SystemPack.from_yaml(_HERE / "telecom.yaml")

BUILTIN_SYSTEMS = (ELEC, FACILITY, GAS, TELECOM)

__all__ = ["BUILTIN_SYSTEMS", "ELEC", "FACILITY", "GAS", "TELECOM"]
