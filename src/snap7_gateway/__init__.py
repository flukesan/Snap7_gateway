"""Snap7 Industrial Gateway.

A 24/7 background service that bridges Siemens S7 PLCs to DeviceWise by
polling real PLCs with a Snap7 *client* and re-hosting the collected data on a
Snap7 *server* that DeviceWise connects to as if it were a real S7 CPU.
"""

__version__ = "0.1.0"
