# PO Tokens

## What this is

YouTube's SABR streaming endpoint sometimes returns
`STREAM_PROTECTION_STATUS=ATTESTATION_REQUIRED`. When that happens,
the server refuses to deliver bytes until the request carries a
**Proof-of-Origin (PO) token** — a base64url string proving the
request came from a real browser session.

PO tokens are produced by a BotGuard JavaScript challenge running in
a real browser. We can't generate them in pure Python; the BotGuard
logic is private and changes frequently. What pyt does instead:

- Detects `ATTESTATION_REQUIRED` and translates it to a typed
  `AttestationRequired` error.
- Lets you plug in any external generator: a static token, a Python
  callable, a shell command, or a JavaScript file.
- Caches the token for the client lifetime so generators don't run
  on every request.
- Auto-retries the download once with a freshly-generated token when
  attestation fails mid-stream.

## Quickest path: auto-install pyt's bundled generator

If you have **Node 18+** on PATH, the doctor can set up an end-to-end
generator with one command:

```bash
pyt --doctor --install po-token-generator
```

This:

1. Verifies `node` and `npm` are on PATH (errors clearly if not — we
   don't auto-install Node; that's your package manager's job).
2. Verifies Node is 18+ (older versions lack the global `fetch` the
   launcher uses).
3. Creates `~/.pyt/js/`, writes a minimal `package.json` there, and
   runs `npm install bgutils-js@^3.2.0 youtubei.js@^14`. Both packages
   are pure-JS BotGuard implementations — no Chrome, no puppeteer.
4. Copies the vendored launcher script (`pyt/data/po_token_launcher.js`)
   into the install dir.
5. Creates a wrapper at `~/.pyt/bin/pyt-po-token` (or `pyt-po-token.cmd`
   on Windows) that runs `node po_token_launcher.js "$@"` with
   `NODE_PATH` pointing at the local `node_modules`. The wrapper
   directory is already on pyt's `PATH`, so `shutil.which` finds it.
6. Runs `pyt-po-token --check` to verify everything links up.

After that, `--po-token-cmd "pyt-po-token"` (or `pyt-po-token <visitor_data>`)
gets you a fresh token. The Client's auto-retry logic handles
regeneration on `ATTESTATION_REQUIRED` automatically.

```bash
# CLI
pyt <url> --po-token-cmd "pyt-po-token"

# Python API
from pyt import Client
client = Client(po_token_cmd="pyt-po-token")
```

If npm install fails, re-run with `pyt -vv --doctor --install
po-token-generator` to see node's stderr — the most common cause is
a missing or corporate-proxy-blocked npm registry.

The generator stops working when YouTube's BotGuard wire format
changes upstream. Both bundled npm packages are actively maintained;
the doctor pins minor versions and bumping the pin (in
`pyt/api/doctor.py`'s `_POT_NPM_PACKAGES` constant) is usually all
that's needed when that happens.

## When you need one

Most public videos work without a PO token. You'll typically hit
`ATTESTATION_REQUIRED` when:

- The video has age / region restrictions
- Your account state has been flagged as bot-like
- You're hitting YouTube from a fresh IP / new session

If `pp.upscale` / `download_best` / `Stream.download` raise
`AttestationRequired`, you need a PO token.

## Three ways to provide a token

### 1. Static token (`po_token=`)

Fastest if you can grab one manually. Open YouTube in your browser,
open DevTools → Network tab, look for a request to
`/youtubei/v1/player`, copy the `poToken` field from the JSON body.

```python
from pyt import Client

client = Client(po_token="abcdef1234...")
video = client.video(url)
video.download_best("./").run()
```

```bash
pyt <url> --po-token "abcdef1234..."
```

The static path doesn't auto-refresh. If the token expires mid-run,
you'll need to grab a new one.

### 2. Python callable (`po_token_provider=`)

Wrap any Python callable that returns a token string. The callable
runs on first use and again when `AttestationRequired` triggers a
retry.

```python
def my_generator():
    # call your existing token generator here
    return fetch_from_internal_service()

client = Client(po_token_provider=my_generator)
```

The provider's result is cached for 30 minutes by default. Pass
`force_refresh=True` on the provider object to bypass the cache.

### 3. Shell command (`po_token_cmd=`)

Drop-in for tools that print a token to stdout. The first non-empty
last line is taken as the token.

```python
client = Client(po_token_cmd="bgutil-pot --visitor-data $VD")
```

```bash
pyt <url> --po-token-cmd "bgutil-pot --visitor-data $VD"
```

`shlex.split` parses string commands; pass a list to skip parsing:

```python
Client(po_token_cmd=["bgutil-pot", "--visitor-data", visitor_data])
```

### 4. JavaScript file (`po_token_script=`)

Runs a JS file with the first available JS runtime on `PATH`
(`node` → `bun` → `deno`, in preference order). The script's stdout
is the token.

```python
client = Client(po_token_script="/path/to/po_token_gen.js")
```

```bash
pyt <url> --po-token-script ~/.local/lib/po_token_gen.js
```

`pyt --doctor` reports which runtimes are installed:

```
$ pyt --doctor
[OK]   node           v20.10.0
       path:    /usr/local/bin/node
       used by: JS runtime for PO-token generators (bgutil-pot, etc.)
```

## How auto-retry works

When `CombinedDownload.run()` (or any download path going through
SABR) hits `ATTESTATION_REQUIRED`:

1. The session translates `SabrAttestationRequired` →
   `AttestationRequired` at the API boundary.
2. If the parent `Client` has a provider configured, we ask it to
   refresh the token (`provider.invalidate()` + `provider.get(force_refresh=True)`).
3. The new token is written to the video's SABR config so the next
   SABR session picks it up.
4. The part files are reset (truncated + reopened) and the SABR
   exchange runs once more.
5. If the second attempt also fails, the error propagates.

This means a typical download fires the generator at most twice: once
to seed the cache, and once if the server rejects the cached token.

## Generators that work today

| Generator | How to install | Use with |
|---|---|---|
| **`pyt-po-token` (bundled)** | `pyt --doctor --install po-token-generator` (needs Node 18+) | `--po-token-cmd "pyt-po-token"` |
| [`bgutil-pot`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) | Their own README | `--po-token-cmd "bgutil-pot ..."` |
| [`youtube-po-token-generator`](https://www.npmjs.com/package/youtube-po-token-generator) | `npm install -g youtube-po-token-generator` | `--po-token-script ~/.local/lib/wrapper.js` |
| Manual extraction (DevTools) | Open YouTube → DevTools → Network → `/youtubei/v1/player` request body | `--po-token <token>` |

The bundled option is the path of least resistance: one command, one
generator, and the version pin lives in pyt itself so we can rev it
when YouTube changes the protocol. Use it unless you have specific
reasons to prefer one of the others.

## Doctor

Run `pyt --doctor` to see what's available for token generation:

```
Tools
------------------------------------------------------------
  [OK]   node           v20.10.0
  [--]   bun            not installed  (install: https://bun.sh)
  [--]   deno           not installed  (install: https://deno.com)
  [--]   bgutil-pot     not installed  (install: https://github.com/Brainicism/bgutil-ytdlp-pot-provider)

Features
------------------------------------------------------------
  [OK]   PO token auto-generation (for ATTESTATION_REQUIRED)
```

The "PO token auto-generation" feature flips to `[OK]` as soon as
*any one* of node / bun / deno / bgutil-pot is on `PATH`. None of
them are required if you're providing tokens manually via
`--po-token`.

## Errors

```python
from pyt import Client, AttestationRequired

try:
    Client().video(url).download_best("./").run()
except AttestationRequired as exc:
    print(f"Need PO token for {exc.video_id} ({exc.url})")
    # Configure a provider and retry
    client = Client(po_token_cmd="bgutil-pot")
    client.video(url).download_best("./").run()
```

The error message itself spells out the three ways to provide a
token, so users hitting it for the first time get inline guidance.

## Limitations

- **We don't generate tokens in pure Python.** BotGuard runs in JS;
  the bundled generator shells out to Node.js. If you don't have Node
  on PATH, fall back to manual extraction or one of the community
  generators.
- **Auto-retry happens at most once.** If the second attempt fails,
  the error propagates so the user can investigate (perhaps the
  generator is broken, the npm install needs updating, or the video
  is genuinely gated beyond what a token can unlock).
- **Only `CombinedDownload` auto-retries.** The CLI's `pyt <url>` command runs
  the generator once at startup and passes the result as a static
  token; if it expires mid-download, re-run.
- **The bundled generator is best-effort.** It uses two pure-JS
  packages (`bgutils-js`, `youtubei.js`) that track YouTube's private
  BotGuard API. When YouTube changes that API, both packages publish
  fixes — bumping pyt's pinned versions (in
  `pyt/api/doctor.py`'s `_POT_NPM_PACKAGES`) restores functionality.
