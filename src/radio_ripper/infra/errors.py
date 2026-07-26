"""Custom exception hierarchy for radio_ripper.

All errors raised by radio_ripper inherit from :class:`RadioRipperError`
so callers can catch the entire family with a single ``except``.
"""

from __future__ import annotations


class RadioRipperError(Exception):
    """Base error for every failure inside radio_ripper."""


class ConfigurationError(RadioRipperError):
    """Raised when the configuration file is missing, invalid, or incomplete."""


class TaggingError(RadioRipperError):
    """Writing ID3 tags to a file failed."""


__all__ = [
    "ConfigurationError",
    "RadioRipperError",
    "TaggingError",
]
