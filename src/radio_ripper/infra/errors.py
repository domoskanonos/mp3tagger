"""Custom exception hierarchy for radio_ripper.

All errors raised by radio_ripper inherit from :class:`RadioRipperError`
so callers can catch the entire family with a single ``except``.
"""

from __future__ import annotations


class RadioRipperError(Exception):
    """Base error for every failure inside radio_ripper."""


class ConfigurationError(RadioRipperError):
    """Raised when the configuration file is missing, invalid, or incomplete."""


class StreamError(RadioRipperError):
    """Base error for any stream-related failure."""


class StreamConnectionError(StreamError):
    """Failed to connect to the stream URL (network, DNS, TLS, HTTP status)."""


class StreamProtocolError(StreamError):
    """The stream violated the expected ICY protocol (bad metaint, oversized metadata)."""


class StreamInterruptedError(StreamError):
    """The stream connection was interrupted mid-read (timeout, reset, EOF)."""


class MetadataProviderError(RadioRipperError):
    """A metadata provider (e.g. iTunes) failed or returned invalid data."""


class TaggingError(RadioRipperError):
    """Writing ID3 tags to a file failed."""


class RepositoryError(RadioRipperError):
    """A database operation failed."""


__all__ = [
    "ConfigurationError",
    "MetadataProviderError",
    "RadioRipperError",
    "RepositoryError",
    "StreamConnectionError",
    "StreamError",
    "StreamInterruptedError",
    "StreamProtocolError",
    "TaggingError",
]
