"""Reusable dependency injected testing components."""

import os
import json
import gzip


import pytest

from pyt import extract


def load_playback_file(filename):
    """Load a gzip json playback file."""
    cur_fp = os.path.realpath(__file__)
    cur_dir = os.path.dirname(cur_fp)
    fp = os.path.join(cur_dir, "mocks", filename)
    with gzip.open(fp, "rb") as fh:
        content = fh.read().decode("utf-8")
        return json.loads(content)


class PlaybackFixture:
    """A recorded watch page, player JS and player response for one video."""

    def __init__(self, recording):
        self.watch_url = recording["url"]
        self.video_id = extract.video_id(self.watch_url)
        self.watch_html = recording["watch_html"]
        self.js = recording["js"]
        self._vid_info = recording["vid_info"]

    @property
    def publish_date(self):
        return extract.publish_date(self.watch_html)


def load_and_init_from_playback_file(filename):
    """Load a gzip json playback file into a :class:`PlaybackFixture`."""
    return PlaybackFixture(load_playback_file(filename))


@pytest.fixture
def cipher_signature():
    """Recording of video id 2lAe1cqCOXo."""
    filename = "yt-video-2lAe1cqCOXo-html.json.gz"
    return load_and_init_from_playback_file(filename)


@pytest.fixture
def presigned_video():
    """Recording of video id QRS8MkLhQmM."""
    filename = "yt-video-QRS8MkLhQmM-html.json.gz"
    return load_and_init_from_playback_file(filename)


@pytest.fixture
def age_restricted():
    """Youtube instance initialized with video id irauhITDrsE."""
    filename = "yt-video-irauhITDrsE-html.json.gz"
    return load_playback_file(filename)


@pytest.fixture
def private():
    """Youtube instance initialized with video id m8uHb5jIGN8."""
    filename = "yt-video-m8uHb5jIGN8-html.json.gz"
    return load_playback_file(filename)


@pytest.fixture
def missing_recording():
    """Youtube instance initialized with video id 5YceQ8YqYMc."""
    filename = "yt-video-5YceQ8YqYMc-html.json.gz"
    return load_playback_file(filename)


@pytest.fixture
def stream_dict():
    """Youtube instance initialized with video id WXxV9g7lsFE."""
    file_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)),
        "mocks",
        "yt-video-WXxV9g7lsFE-html.json.gz",
    )
    with gzip.open(file_path, "rb") as f:
        content = json.loads(f.read().decode("utf-8"))
        return content["watch_html"]


@pytest.fixture
def base_js():
    """Youtube base.js files retrieved on 2022-02-04 and 2022-04-15
    from https://www.youtube.com/watch?v=vmzxpUsN0uA and
    https://www.youtube.com/watch?v=Y4-GSFKZmEg respectively
    """
    base_js_files = []
    for file in ["base.js-2022-02-04.gz", "base.js-2022-04-15.gz"]:
        file_path = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            "mocks",
            file,
        )
        with gzip.open(file_path, "rb") as f:
            base_js_files.append(f.read().decode("utf-8"))
    return base_js_files
