from .dahua import DahuaPlugin
from .generic import GenericPlugin
from .hikvision import HikvisionPlugin
from .honeywell import HoneywellPlugin

NATIVE = [HikvisionPlugin(), DahuaPlugin(), HoneywellPlugin()]
UNIVERSAL = GenericPlugin()
