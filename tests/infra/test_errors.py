"""Tests for radio_ripper.infra.errors."""

from __future__ import annotations

import pytest

from radio_ripper.infra.errors import ConfigurationError, RadioRipperError, TaggingError


@pytest.mark.parametrize(
    "exc_cls",
    [
        ConfigurationError,
        TaggingError,
    ],
)
def test_all_inherit_base(exc_cls):
    assert issubclass(exc_cls, RadioRipperError)


def test_raisable_and_caught_as_base():
    with pytest.raises(RadioRipperError):
        raise ConfigurationError("x")
