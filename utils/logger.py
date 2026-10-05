"""Logger factory — configures dual file+console logging with a standard format."""

import logging
import os
from logging import Logger
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(name: str, log_dir: str, level: str = "INFO") -> Logger:
    """Create and return a Logger that writes to both a file and the console.

    The file handler always captures DEBUG and above so nothing is lost on
    disk.  The console handler respects the ``level`` argument so noisy
    DEBUG lines stay out of the terminal unless requested.

    Calling this function twice with the same ``name`` is safe — handlers are
    not duplicated on an already-configured logger.

    Log format::

        2026-07-06 12:00:00 | INFO     | test_harness | message here

    Args:
        name: Logger name; also used as the base name of the ``.log`` file.
        log_dir: Directory where ``{name}.log`` is written.  Created if absent.
        level: Minimum level for console output (``DEBUG``, ``INFO``,
               ``WARNING``, ``ERROR``).  Case-insensitive.

    Returns:
        Fully configured :class:`logging.Logger` instance.

    Raises:
        OSError: If ``log_dir`` cannot be created or is not writable.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)

    # Guard against duplicate handlers when called more than once per process.
    if logger.handlers:
        return logger

    # Logger itself captures everything; handlers filter individually.
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # File handler — DEBUG and above, UTF-8 so Unicode in agent responses is safe.
    log_file = os.path.join(log_dir, f"{name}.log")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Console handler — at the caller-requested level.
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
