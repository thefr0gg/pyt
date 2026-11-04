import os

from unittest import mock
from unittest.mock import MagicMock, mock_open, patch

from pyt import captions
from pyt.captions import Caption


def test_float_to_srt_time_format():
    caption1 = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    assert caption1.float_to_srt_time_format(3.89) == "00:00:03,890"


@mock.patch("pyt.captions.Caption.generate_srt_captions")
def test_download(srt):
    open_mock = mock_open()
    with patch("builtins.open", open_mock):
        srt.return_value = ""
        caption = Caption(
            {
                "url": "url1",
                "name": {"simpleText": "name1"},
                "languageCode": "en",
                "vssId": ".en",
            }
        )
        caption.download("title")
        assert (
            open_mock.call_args_list[0][0][0].split(os.path.sep)[-1] == "title (en).srt"
        )


@mock.patch("pyt.captions.Caption.generate_srt_captions")
def test_download_with_prefix(srt):
    open_mock = mock_open()
    with patch("builtins.open", open_mock):
        srt.return_value = ""
        caption = Caption(
            {
                "url": "url1",
                "name": {"simpleText": "name1"},
                "languageCode": "en",
                "vssId": ".en",
            }
        )
        caption.download("title", filename_prefix="1 ")
        assert (
            open_mock.call_args_list[0][0][0].split(os.path.sep)[-1]
            == "1 title (en).srt"
        )


@mock.patch("pyt.captions.Caption.generate_srt_captions")
def test_download_with_output_path(srt):
    open_mock = mock_open()
    captions.target_directory = MagicMock(return_value="/target")
    with patch("builtins.open", open_mock):
        srt.return_value = ""
        caption = Caption(
            {
                "url": "url1",
                "name": {"simpleText": "name1"},
                "languageCode": "en",
                "vssId": ".en",
            }
        )
        file_path = caption.download("title", output_path="blah")
        assert file_path == os.path.join("/target", "title (en).srt")
        captions.target_directory.assert_called_with("blah")


@mock.patch("pyt.captions.Caption.xml_captions")
def test_download_xml_and_trim_extension(xml):
    open_mock = mock_open()
    with patch("builtins.open", open_mock):
        xml.return_value = ""
        caption = Caption(
            {
                "url": "url1",
                "name": {"simpleText": "name1"},
                "languageCode": "en",
                "vssId": ".en",
            }
        )
        caption.download("title.xml", srt=False)
        assert (
            open_mock.call_args_list[0][0][0].split(os.path.sep)[-1] == "title (en).xml"
        )


def test_repr():
    caption = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    assert str(caption) == '<Caption lang="name1" code="en">'


@mock.patch("pyt.request.get")
def test_xml_captions(request_get):
    request_get.return_value = "test"
    caption = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    assert caption.xml_captions == "test"


@mock.patch("pyt.captions.request")
def test_generate_srt_captions(request):
    request.get.return_value = (
        '<?xml version="1.0" encoding="utf-8" ?><transcript><text start="6.5" dur="1.7">['
        'Herb, Software Engineer]\n本影片包含隱藏式字幕。</text><text start="8.3" dur="2.7">'
        "如要啓動字幕，請按一下這裡的圖示。</text></transcript>"
    )
    caption = Caption(
        {
            "url": "url1",
            "name": {"simpleText": "name1"},
            "languageCode": "en",
            "vssId": ".en",
        }
    )
    assert caption.generate_srt_captions() == (
        "1\n"
        "00:00:06,500 --> 00:00:08,200\n"
        "[Herb, Software Engineer] 本影片包含隱藏式字幕。\n"
        "\n"
        "2\n"
        "00:00:08,300 --> 00:00:11,000\n"
        "如要啓動字幕，請按一下這裡的圖示。"
    )
