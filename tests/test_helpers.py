import os

from unittest import mock

import pytest
from pyt import helpers
from pyt.exceptions import RegexMatchError
from pyt.helpers import cache, deprecated, setup_logger
from pyt.helpers import target_directory, uniqueify


def test_regex_search_no_match():
    with pytest.raises(RegexMatchError):
        helpers.regex_search("^a$", "", group=0)


def test_regex_search():
    assert helpers.regex_search("^a$", "a", group=0) == "a"


def test_safe_filename():
    """Unsafe characters get stripped from generated filename"""
    assert helpers.safe_filename("abc1245$$") == "abc1245"
    assert helpers.safe_filename("abc##") == "abc"


@mock.patch("warnings.warn")
def test_deprecated(warn):
    @deprecated("oh no")
    def deprecated_function():
        return None

    deprecated_function()
    warn.assert_called_with(
        "Call to deprecated function deprecated_function (oh no).",
        category=DeprecationWarning,
        stacklevel=2,
    )


def test_cache():
    call_count = 0

    @cache
    def cached_func(stuff):
        nonlocal call_count
        call_count += 1
        return stuff

    cached_func("hi")
    cached_func("hi")
    cached_func("bye")
    cached_func("bye")

    assert call_count == 2


@mock.patch("os.path.isabs", return_value=False)
@mock.patch("os.getcwd", return_value="/cwd")
@mock.patch("os.makedirs")
def test_target_directory_with_relative_path(_, __, makedirs):  # noqa: PT019
    assert target_directory("test") == os.path.join("/cwd", "test")
    makedirs.assert_called()


@mock.patch("os.path.isabs", return_value=True)
@mock.patch("os.makedirs")
def test_target_directory_with_absolute_path(_, makedirs):  # noqa: PT019
    assert target_directory("/test") == "/test"
    makedirs.assert_called()


@mock.patch("os.getcwd", return_value="/cwd")
@mock.patch("os.makedirs")
def test_target_directory_with_no_path(_, makedirs):  # noqa: PT019
    assert target_directory() == "/cwd"
    makedirs.assert_called()


@mock.patch("pyt.helpers.logging")
def test_setup_logger(logging):
    # Given
    logger = logging.getLogger.return_value
    # When
    setup_logger(20)
    # Then
    logging.getLogger.assert_called_with("pyt")
    logger.addHandler.assert_called()
    logger.setLevel.assert_called_with(20)


def test_uniqueify():
    non_unique_list = [1, 2, 3, 3, 4, 5]
    expected = [1, 2, 3, 4, 5]
    result = uniqueify(non_unique_list)
    assert result == expected
