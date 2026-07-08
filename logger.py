"""
Logger configuration.
"""

import logging
import sys
from config import LOG_LEVEL

# Create logger
logger = logging.getLogger("stamp_identifier")
logger.setLevel(getattr(logging, LOG_LEVEL))

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(getattr(logging, LOG_LEVEL))

# Formatter
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler.setFormatter(formatter)

# Add handler
if not logger.handlers:
    logger.addHandler(console_handler)

# Prevent propagation
logger.propagate = False
