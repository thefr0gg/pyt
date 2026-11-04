# Cookie Authentication

Supply YouTube session cookies to access age-restricted, members-only,
or otherwise authenticated content.

Equivalent to yt-dlp's `--cookies` and `--cookies-from-browser`.

## When you need cookies

| Situation | Symptom | Fix |
|---|---|---|
| Age-restricted video | `AgeRestricted` exception | cookies from signed-in, age-verified account |
| Members-only video | `VideoUnavailable(reason="members-only")` | cookies from a subscribed account |
| Private video | `VideoUnavailable(reason="private")` | cookies from an account that has access |

You can detect age-gated content before attempting a download:

```python
video = client.video(url)
if video.is_age_restricted:
    print("This video needs cookies — reconstruct Client with cookies_from_browser=")
```

## Requirements

`browser-cookie3` (browser extraction) and Python stdlib `http.cookiejar`
(file-based cookies) are both available out of the box — no extra install
step.

## CLI Usage

### From a cookie file

Export your YouTube cookies in Netscape format using a browser extension
such as "Get cookies.txt LOCALLY" (Chrome/Firefox), then:

```bash
pyt <url> --cookies ~/youtube-cookies.txt
```

### From a browser (auto-extraction)

```bash
pyt <url> --cookies-from-browser chrome
pyt <url> --cookies-from-browser firefox
pyt <url> --cookies-from-browser brave
pyt <url> --cookies-from-browser edge
pyt <url> --cookies-from-browser safari
```

The cookies are extracted from the browser's local cookie store for
`.youtube.com` only. The browser must be installed and you must be
signed in; on macOS the Keychain prompt may appear the first time.

## Python API (modern)

The recommended path passes cookies through `Client`:

```python
from pyt import Client

# Extract live cookies from a running Chrome profile
client = Client(cookies_from_browser="chrome")

# Or point at a Netscape-format cookie file
client = Client(cookies="~/youtube-cookies.txt")

video = client.video("https://www.youtube.com/watch?v=<age_restricted_id>")
video.streams.audio.best().download_to("downloads/").run()
```

Cookies are installed once per `Client` instance and apply to every
video fetched through it.

## Low-level cookie helpers

```python
from pyt.cookies import install_cookies, load_cookies_from_file, load_cookies_from_browser

# From file
jar = load_cookies_from_file("cookies.txt")
install_cookies(jar)

# From browser (requires browser-cookie3)
jar = load_cookies_from_browser("firefox")
install_cookies(jar)
```

`install_cookies()` must be called before any video is fetched.
Prefer `Client(cookies_from_browser=...)`, which installs them for you.

## Cookie File Format

Netscape format (compatible with yt-dlp):

```
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	FALSE	0	SAPISID	<value>
.youtube.com	TRUE	/	FALSE	0	__Secure-3PAPISID	<value>
```

## Security Note

Cookie files contain session credentials. Keep them private and do not
commit them to version control. Add `cookies.txt` (or whatever you name
it) to `.gitignore`.
