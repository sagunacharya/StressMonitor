"""Test-only dependency shims for unavailable physical serial hardware libraries."""
import importlib.util
import sys
import types

# Parsing/window unit tests do not open serial devices. A minimal import shim
# lets those tests run in an offline developer container without pyserial;
# the production requirements and CI still install the real package.
if importlib.util.find_spec("serial") is None:
    serial_stub = types.ModuleType("serial")
    class SerialException(Exception):
        pass
    serial_stub.SerialException = SerialException
    serial_stub.Serial = object
    sys.modules["serial"] = serial_stub
