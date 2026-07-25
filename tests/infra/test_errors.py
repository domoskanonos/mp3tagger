"""Tests for radio_ripper.infra.errors."""

from __future__ import annotations

import pytest

from radio_ripper.infra.errors import (
    ConfigurationError,
    MetadataProviderError,
    RadioRipperError,
    RepositoryError,
    StreamConnectionError,
    StreamError,
    StreamInterruptedError,
    StreamProtocolError,
    TaggingError,
)


@pytest.mark.parametrize(
    "exc_cls",
    [
        ConfigurationError,
        StreamConnectionError,
        StreamProtocolError,
        StreamInterruptedError,
        MetadataProviderError,
        TaggingError,
        RepositoryError,
    ],
)
def test_all_inherit_base(exc_cls):
    assert issubclass(exc_cls, RadioRipperError)


def test_stream_errors_inherit_stream_base():
    for exc_cls in (StreamConnectionError, StreamProtocolError, StreamInterruptedError):
        assert issubclass(exc_cls, StreamError)


def test_raisable_and_caught_as_base():
    with pytest.raises(RadioRipperError):
        raise ConfigurationError("x")
