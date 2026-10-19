"""
Configuration for the command-line tools.
"""
import os

# Disk caching of third-party API responses (GitHub) in ./cache. Off unless explicitly enabled:
# cached data is personal data, and nothing should be written to disk by default.
DEVELOPMENT_MODE = os.getenv("KUKHIRE_CLI_DEV_MODE", "") == "1"
