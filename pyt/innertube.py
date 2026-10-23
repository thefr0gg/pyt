"""This module is designed to interact with the innertube API.

This module is NOT intended to be used directly by end users, as each of the
interfaces returns raw results. These should instead be parsed to extract
the useful information for the end user.
"""

import os
import json
import time
import pathlib

from urllib import parse

from pyt import request

# YouTube on TV client secrets
_client_id = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
_client_secret = "SboVhoG9s0rNafixCSGGKXAT"

# Extracted API keys -- unclear what these are linked to.
_api_keys = [
    "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    "AIzaSyCtkvNIR1HCEwzsqK6JuE6KqpyjusIRI30",
    "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
    "AIzaSyC8UYZpvA2eknNex0Pjid0_eTLJoDu6los",
    "AIzaSyCjc_pVEDi4qsv5MtC2dMXzpIaDoRFLsxw",
    "AIzaSyDHQ9ipnphqTzDqZsbtd8_Ru4_kiKVQe2k",
]

_default_clients = {
    "WEB": {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20241127.01.00",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    # ANDROID_VR at version ≤1.65 bypasses SABR-only stream enforcement and
    # returns direct signed URLs for all adaptive formats (same approach as
    # yt-dlp's android_vr client). Versions >1.65 return SABR-only streams.
    "ANDROID_VR": {
        "context": {
            "client": {
                "clientName": "ANDROID_VR",
                "clientVersion": "1.65.10",
                "deviceMake": "Oculus",
                "deviceModel": "Quest 3",
                "androidSdkVersion": 32,
                "hl": "en",
                "userAgent": "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
                "osName": "Android",
                "osVersion": "12L",
            }
        },
        "header": {
            "User-Agent": "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
            "X-Youtube-Client-Name": "28",
            "X-Youtube-Client-Version": "1.65.10",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    # VISIONOS does not require a PO token (same tier as ANDROID_VR) and
    # is yt-dlp's current fallback for videos where ANDROID_VR's player
    # response looks OK but the SABR session it returns never actually
    # delivers media (observed on real traffic: repeated backoff/reload
    # signals with no MEDIA_HEADER ever arriving).
    "VISIONOS": {
        "context": {
            "client": {
                "clientName": "VISIONOS",
                "clientVersion": "1.02",
                "deviceMake": "Apple",
                "deviceModel": "RealityDevice17,1",
                "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
                "osName": "visionOS",
                "osVersion": "26.5.23O471",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
            "X-Youtube-Client-Name": "101",
            "X-Youtube-Client-Version": "1.02",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "ANDROID": {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "21.26.364",
                "androidSdkVersion": 30,
                "hl": "en",
                "userAgent": "com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip",
                "osName": "Android",
                "osVersion": "11",
            }
        },
        "header": {
            "User-Agent": "com.google.android.youtube/21.26.364 (Linux; U; Android 11) gzip",
            "X-Youtube-Client-Name": "3",
            "X-Youtube-Client-Version": "21.26.364",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "IOS": {
        "context": {
            "client": {
                "clientName": "IOS",
                "clientVersion": "21.26.4",
                "deviceMake": "Apple",
                "deviceModel": "iPhone16,2",
                "hl": "en",
                "userAgent": "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
            }
        },
        "header": {
            "User-Agent": "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
            "X-Youtube-Client-Name": "5",
            "X-Youtube-Client-Version": "21.26.4",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "WEB_EMBED": {
        "context": {
            "client": {
                "clientName": "WEB_EMBEDDED_PLAYER",
                "clientVersion": "2.20241127.01.00",
                "clientScreen": "EMBED",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "ANDROID_EMBED": {
        "context": {
            "client": {
                "clientName": "ANDROID_EMBEDDED_PLAYER",
                "clientVersion": "21.16.7",
                "clientScreen": "EMBED",
                "androidSdkVersion": 34,
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "com.google.android.youtube/21.16.7 (Linux; U; Android 14; en_US; Pixel 8; Build/UQ1A.240205.004; Cronet/113.0.5672.24)",
            "X-Youtube-Client-Name": "55",
            "X-Youtube-Client-Version": "21.16.7",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "IOS_EMBED": {
        "context": {
            "client": {
                "clientName": "IOS_MESSAGES_EXTENSION",
                "clientVersion": "19.45.4",
                "deviceModel": "iPhone16,2",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "com.google.ios.youtube/19.45.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "WEB_MUSIC": {
        "context": {
            "client": {
                "clientName": "WEB_REMIX",
                "clientVersion": "1.20241127.01.00",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "ANDROID_MUSIC": {
        "context": {
            "client": {
                "clientName": "ANDROID_MUSIC",
                "clientVersion": "7.27.52",
                "androidSdkVersion": 34,
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "com.google.android.apps.youtube.music/7.27.52 (Linux; U; Android 14) gzip",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "IOS_MUSIC": {
        "context": {
            "client": {
                "clientName": "IOS_MUSIC",
                "clientVersion": "7.27.0",
                "deviceModel": "iPhone16,2",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "com.google.ios.youtubemusic/7.27.0 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "WEB_CREATOR": {
        "context": {
            "client": {
                "clientName": "WEB_CREATOR",
                "clientVersion": "1.20241127.01.00",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "ANDROID_CREATOR": {
        "context": {
            "client": {
                "clientName": "ANDROID_CREATOR",
                "clientVersion": "24.45.100",
                "androidSdkVersion": 34,
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "com.google.android.apps.youtube.creator/24.45.100 (Linux; U; Android 14) gzip",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "IOS_CREATOR": {
        "context": {
            "client": {
                "clientName": "IOS_CREATOR",
                "clientVersion": "24.45.100",
                "deviceModel": "iPhone16,2",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "com.google.ios.ytcreator/24.45.100 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "MWEB": {
        "context": {
            "client": {
                "clientName": "MWEB",
                "clientVersion": "2.20241127.00.00",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
    "TV_EMBED": {
        "context": {
            "client": {
                "clientName": "TVHTML5_SIMPLY_EMBEDDED_PLAYER",
                "clientVersion": "2.0",
                "hl": "en",
            }
        },
        "header": {
            "User-Agent": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/538.1 (KHTML, like Gecko) Version/6.5 TV Safari/538.1",
        },
        "api_key": "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
    },
}
_token_timeout = 1800
_cache_dir = pathlib.Path(__file__).parent.resolve() / "__cache__"
_token_file = os.path.join(_cache_dir, "tokens.json")


class InnerTube:
    """Object for interacting with the innertube API."""

    def __init__(
        self, client="ANDROID", use_oauth=False, allow_cache=True, client_version=None
    ):
        """Initialize an InnerTube object.

        :param str client:
            Client to use for the object.
        :param bool use_oauth:
            Whether or not to authenticate to YouTube.
        :param bool allow_cache:
            Allows caching of oauth tokens on the machine.
        :param str client_version:
            Optional version string to override the hardcoded client version.
            Useful for web-based clients whose version can be read from the page.
        """
        self.client_name = client
        self.context = _default_clients[client]["context"]
        self.header = _default_clients[client]["header"]
        self.api_key = _default_clients[client]["api_key"]

        if client_version:
            import copy

            self.context = copy.deepcopy(self.context)
            self.context["client"]["clientVersion"] = client_version
        self.access_token = None
        self.refresh_token = None
        self.use_oauth = use_oauth
        self.allow_cache = allow_cache

        # Stored as epoch time
        self.expires = None

        # Try to load from file if specified
        if self.use_oauth and self.allow_cache:
            # Try to load from file if possible
            if os.path.exists(_token_file):
                with open(_token_file) as f:
                    data = json.load(f)
                    self.access_token = data["access_token"]
                    self.refresh_token = data["refresh_token"]
                    self.expires = data["expires"]
                    self.refresh_bearer_token()

    def cache_tokens(self):
        """Cache tokens to file if allowed."""
        if not self.allow_cache:
            return

        data = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires": self.expires,
        }
        if not os.path.exists(_cache_dir):
            os.mkdir(_cache_dir)
        with open(_token_file, "w") as f:
            json.dump(data, f)

    def refresh_bearer_token(self, force=False):
        """Refreshes the OAuth token if necessary.

        :param bool force:
            Force-refresh the bearer token.
        """
        if not self.use_oauth:
            return
        # Skip refresh if it's not necessary and not forced
        if self.expires > time.time() and not force:
            return

        # Subtracting 30 seconds is arbitrary to avoid potential time discrepencies
        start_time = int(time.time() - 30)
        data = {
            "client_id": _client_id,
            "client_secret": _client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        response = request._execute_request(
            "https://oauth2.googleapis.com/token",
            "POST",
            headers={"Content-Type": "application/json"},
            data=data,
        )
        response_data = json.loads(response.read())

        self.access_token = response_data["access_token"]
        self.expires = start_time + response_data["expires_in"]
        self.cache_tokens()

    def fetch_bearer_token(self):
        """Fetch an OAuth token."""
        # Subtracting 30 seconds is arbitrary to avoid potential time discrepencies
        start_time = int(time.time() - 30)
        data = {
            "client_id": _client_id,
            "scope": "https://www.googleapis.com/auth/youtube",
        }
        response = request._execute_request(
            "https://oauth2.googleapis.com/device/code",
            "POST",
            headers={"Content-Type": "application/json"},
            data=data,
        )
        response_data = json.loads(response.read())
        verification_url = response_data["verification_url"]
        user_code = response_data["user_code"]
        print(f"Please open {verification_url} and input code {user_code}")
        input("Press enter when you have completed this step.")

        data = {
            "client_id": _client_id,
            "client_secret": _client_secret,
            "device_code": response_data["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }
        response = request._execute_request(
            "https://oauth2.googleapis.com/token",
            "POST",
            headers={"Content-Type": "application/json"},
            data=data,
        )
        response_data = json.loads(response.read())

        self.access_token = response_data["access_token"]
        self.refresh_token = response_data["refresh_token"]
        self.expires = start_time + response_data["expires_in"]
        self.cache_tokens()

    @property
    def base_url(self):
        """Return the base url endpoint for the innertube API."""
        return "https://www.youtube.com/youtubei/v1"

    @property
    def base_data(self):
        """Return the base json data to transmit to the innertube API."""
        return {"context": self.context}

    @property
    def base_params(self):
        """Return the base query parameters to transmit to the innertube API."""
        return {
            "prettyPrint": False,
        }

    @property
    def base_body(self):
        """Return base fields merged into POST body for innertube requests."""
        return {
            "contentCheckOk": True,
            "racyCheckOk": True,
        }

    def _call_api(self, endpoint, query, data):
        """Make a request to a given endpoint with the provided query parameters and data."""
        endpoint_url = f"{endpoint}?{parse.urlencode(query)}" if query else endpoint
        headers = {
            "Content-Type": "application/json",
        }
        # Add the bearer token if applicable
        if self.use_oauth:
            if self.access_token:
                self.refresh_bearer_token()
                headers["Authorization"] = f"Bearer {self.access_token}"
            else:
                self.fetch_bearer_token()
                headers["Authorization"] = f"Bearer {self.access_token}"

        headers.update(self.header)

        response = request._execute_request(
            endpoint_url, "POST", headers=headers, data=data
        )
        return json.loads(response.read())

    def browse(self, browse_id=None, *, params=None, continuation=None):
        """Make a request to the browse endpoint.

        Used for channel pages and playlists. Either ``browse_id`` (a
        channel id, ``VL``-prefixed playlist id, etc.) with optional
        ``params`` (an opaque tab selector, e.g. the Videos tab), or
        ``continuation`` (a token from a previous browse/next response)
        to page through a list — never both.

        Unlike :meth:`player` and :meth:`search`, this endpoint rejects
        requests with no ``key`` query parameter, so it's added here.

        :param str browse_id:
            A channel id or ``VL``-prefixed playlist id.
        :param str params:
            Opaque tab-selector params from a channel's tab endpoint.
        :param str continuation:
            A continuation token to page through a list.
        :rtype: dict
        """
        endpoint = f"{self.base_url}/browse"
        data = {}
        if continuation:
            data["continuation"] = continuation
        else:
            data["browseId"] = browse_id
            if params:
                data["params"] = params
        data.update(self.base_data)
        query = dict(self.base_params)
        query["key"] = self.api_key
        return self._call_api(endpoint, query, data)

    def resolve_url(self, url):
        """Resolve a canonical/vanity URL (e.g. a channel handle) to its
        navigation endpoint, which carries the real channel id.

        :param str url:
            A full YouTube URL, e.g. ``https://www.youtube.com/@handle``.
        :rtype: dict
        """
        endpoint = f"{self.base_url}/navigation/resolve_url"
        data = {"url": url}
        data.update(self.base_data)
        query = dict(self.base_params)
        query["key"] = self.api_key
        return self._call_api(endpoint, query, data)

    def config(self):
        """Make a request to the config endpoint.

        TODO: Figure out how we can use this
        """
        # endpoint = f'{self.base_url}/config'  # noqa:E800
        ...
        # return self._call_api(endpoint, query, self.base_data)  # noqa:E800

    def guide(self):
        """Make a request to the guide endpoint.

        TODO: Figure out how we can use this
        """
        # endpoint = f'{self.base_url}/guide'  # noqa:E800
        ...
        # return self._call_api(endpoint, query, self.base_data)  # noqa:E800

    def next(self, *, continuation=None):
        """Make a request to the next endpoint.

        Used to page through a watch-next list; not needed for channel
        or playlist paging (those use :meth:`browse` with a continuation
        token instead).

        :param str continuation:
            A continuation token.
        :rtype: dict
        """
        endpoint = f"{self.base_url}/next"
        data = {}
        if continuation:
            data["continuation"] = continuation
        data.update(self.base_data)
        query = dict(self.base_params)
        query["key"] = self.api_key
        return self._call_api(endpoint, query, data)

    def player(self, video_id, visitor_data=None):
        """Make a request to the player endpoint.

        :param str video_id:
            The video id to get player info for.
        :param str visitor_data:
            Optional visitorData token extracted from the watch page, used to
            satisfy YouTube's precondition checks on mobile clients.
        :rtype: dict
        :returns:
            Raw player info results.
        """
        endpoint = f"{self.base_url}/player"
        data = {
            "videoId": video_id,
        }
        data.update(self.base_body)
        context = dict(self.base_data)
        if visitor_data:
            context["context"] = dict(context.get("context", {}))
            context["context"]["client"] = dict(context["context"].get("client", {}))
            context["context"]["client"]["visitorData"] = visitor_data
        data.update(context)
        return self._call_api(endpoint, self.base_params, data)

    def search(self, search_query, continuation=None):
        """Make a request to the search endpoint.

        :param str search_query:
            The query to search.
        :rtype: dict
        :returns:
            Raw search query results.
        """
        endpoint = f"{self.base_url}/search"
        data = {
            "query": search_query,
        }
        if continuation:
            data["continuation"] = continuation
        data.update(self.base_body)
        data.update(self.base_data)
        return self._call_api(endpoint, self.base_params, data)

    def verify_age(self, video_id):
        """Make a request to the age_verify endpoint.

        Notable examples of the types of video this verification step is for:
        * https://www.youtube.com/watch?v=QLdAhwSBZ3w
        * https://www.youtube.com/watch?v=hc0ZDaAZQT0

        :param str video_id:
            The video id to get player info for.
        :rtype: dict
        :returns:
            Returns information that includes a URL for bypassing certain restrictions.
        """
        endpoint = f"{self.base_url}/verify_age"
        data = {
            "nextEndpoint": {"urlEndpoint": {"url": f"/watch?v={video_id}"}},
            "setControvercy": True,
        }
        data.update(self.base_data)
        result = self._call_api(endpoint, self.base_params, data)
        return result

    def get_transcript(self, video_id):
        """Make a request to the get_transcript endpoint.

        This is likely related to captioning for videos, but is currently untested.
        """
        endpoint = f"{self.base_url}/get_transcript"
        data = {
            "videoId": video_id,
        }
        data.update(self.base_data)
        result = self._call_api(endpoint, self.base_params, data)
        return result
