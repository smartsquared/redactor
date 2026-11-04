# Downloading statements with `redactor fetch`

`redactor fetch` opens a visible Chrome window, waits for you to log in, and records or replays the clicks that reach a statement export. No model is involved.

```bash
pip install 'redactor[fetch]'                                  # uses your installed Chrome
redactor fetch record --site bank --url https://example.test/login   # once
redactor fetch run --site bank                                 # each time after that
redactor fetch status                                          # recordings and files, metadata only
```

Press Enter in the terminal once you've logged in. The sakuma-finance app provides a button for this instead.

## What it stores

| Path | Contents |
|---|---|
| `~/.redactor/fetch/<site>.json` | The recording: login address, clicks and dropdown choices. Can contain account labels. Owner-only. |
| `~/.redactor/fetch/profiles/<site>/` | A Chrome profile for that site, including its login session. Owner-only. |
| `~/.redactor/downloads/` | Downloaded files, renamed so the bank's file name is dropped. Owner-only. |

None of these belong in a repo.

## What it promises

- It never handles your password. Listeners are installed only after you confirm login.
- It never records typed text, only clicks and dropdown choices.
- Output names a site, a step number or a file name, never a selector, address or page text.
- If a step can't be found, it stops and exits non-zero.

## Downloads don't go to the inbox

Bank exports can contain the account's full history and `ingest` isn't idempotent ([#45](https://github.com/smartsquared/redactor/issues/45)). Filter a download to new rows before ingesting it.
