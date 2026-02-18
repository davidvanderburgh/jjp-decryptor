"""Double-click launcher for JJP Asset Decryptor (no console window)."""

import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jjp_decryptor.app import App

App().run()
