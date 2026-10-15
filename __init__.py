"""Register a minimal toolbar bridge without loading analysis dependencies."""
import os

if os.name == 'nt':
    from .toolbar_bridge import KiPIDAToolbarBridge
    KiPIDAToolbarBridge().register()
