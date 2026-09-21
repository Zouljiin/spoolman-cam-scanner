# Managing printers from the web page

The live web page can add, edit, test and remove printers without touching `config.json` or restarting the scanner.
Changes take effect immediately and are saved to the config file.

This is **off by default** because it is the one part of the web page that can change things.

## Turn it on

Add one setting to the `web` section of `config.json`, then restart the scanner once:

```json
"web": {
  "enabled": true,
  "port": 8090,
  "allow_edit": true
}
```

The log confirms it: `web page listening on ... (printer editing ENABLED (no token))`.

**Optional password.** If other people or devices can reach the page and you don't want them changing your printer list, also
set an `edit_token`. The page then asks for it once per browser tab:

```json
"web": { "enabled": true, "port": 8090, "allow_edit": true, "edit_token": "put-a-long-random-string-here" }
```

Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.

## Use it

Open the page and click **Manage printers** (the button only appears when editing is enabled).

- **Add printer**: type the Moonraker address (`192.168.1.50` is expanded to `http://192.168.1.50:7125`), then click
  **Auto detect**.
- **Auto detect**: checks that Moonraker is reachable, that it has the `[spoolman]` component and that the camera returns a
  picture, and **fills in the form**: the tidied-up address, the name (from Klipper's hostname), the camera snapshot URL and the
  webcam name. It only fills fields you left empty, so anything you typed is kept. It saves nothing; click **Save** when happy.
  If something is wrong (unreachable, no Spoolman component, no camera) it says what, and still fills what it could find.
- **Edit**: change any field, including the name or IP address. Leave the API key blank to keep the current one.
- **Remove**: stops watching that printer and deletes it from the config.

Optional fields (snapshot URL, webcam name, Spoolman location, log color, beep macros, API key) match [configuration.md](configuration.md).

**Beep macros** is a checkbox, on by default. It only matters if you set up beeps (see [feedback.md](feedback.md)): untick it for a printer that
doesn't have the macros installed, so its console doesn't show `Unknown command` on every scan. The scanner works either way. Under the hood it
writes empty macro names into that printer's `feedback` in `config.json`, and ticking it again removes them.

**Log color** is the color of that printer's name in the log and on its card. Leave it on **Auto** and a distinct color is
chosen by the printer's position in the list (spread around the color wheel, and darker on the light theme), or click one of the
preset swatches or use the color picker for any color. **Auto** switches back. The list of printers in this window shows each
printer's color too.

**Spoolman location** is a drop-down of the locations Spoolman already has, read live when the form opens. *Default (printer
name)* leaves the setting empty, so spools loaded on this printer get its name as their location. *Other* lets you type a
new name. A location saved earlier that Spoolman no longer lists is still shown, so editing never silently drops it. The list
is read through the printers you already have (or, for your very first printer, through the address you typed). If Spoolman's
`/v1/location` endpoint isn't available, the list is built from the locations found on your spools instead, in which case a
location that exists in Spoolman but has no spools yet won't be listed (use *Other*).
Global settings (polling, feedback, storage location, ...) are not editable here; change them in `config.json` and restart.

## What gets written

- Only the `"printers"` list of `config.json` is rewritten; every other setting is kept. The file is re-saved with
  2-space indentation (JSON has no comments, so nothing is lost).
- The first time it saves, the original is copied to `config.json.bak`.
- Per-printer settings the form doesn't show (for example a per-printer `feedback` override) are preserved on edit.
- **Don't hand-edit the printers list while the scanner is running**: the next save from the page would overwrite your
  edit. Stop the scanner (or restart it afterwards) if you edit the file directly.

## Security

Without a token, **anyone who can open the page can add, change or remove printers** (and make the scanner contact any
address it can reach). That matches how most home-lab tools behave on a trusted network, but consider a token if the page
is reachable from guest Wi-Fi or beyond your home network. Never expose the port to the internet.

- With a token set, every edit request needs `Authorization: Bearer <token>`; wrong tokens get a 401 after a short delay.
  The token travels **unencrypted** over plain HTTP; use an HTTPS reverse proxy (or `"host": "127.0.0.1"` behind one) if
  that matters to you.
- Protection against other websites: every edit request must carry an `X-Scanner-Client: web` header, and the server never
  answers CORS preflights for the edit endpoints. So a malicious page open in your browser can't make your browser change
  your printer list.
- API keys are never sent back to the page.

## API (for scripting)

Every request needs the header `X-Scanner-Client: web`, plus `Authorization: Bearer <edit_token>` if you set a token.
Names in URLs must be URL-encoded.

| Method and path | Body | Effect |
|---|---|---|
| `GET /api/config` | | List printers (`has_api_key` says whether a key is set; the key itself is never returned) |
| `POST /api/printers` | `{name, moonraker, snapshot_url?, webcam?, location?, color?, beeps_off?, api_key?}` (`color` is `#rrggbb`, empty = automatic; `beeps_off: true` switches the beep macros off for this printer) | Add and start watching |
| `PUT /api/printers/<name>` | same fields | Update (restarts that printer's watcher) |
| `DELETE /api/printers/<name>` | | Remove |
| `GET /api/locations` | optional `?moonraker=<address>` | Spoolman's location names (`{"locations": [...], "source": "<printer>"}`), read through an existing printer, or the given address if there are none |
| `POST /api/printers/test` | same fields, plus optional `original` (name of the printer being edited) | Auto detect without saving. Returns `ok`, `error`, `moonraker` (tidied address), `name` (Klipper hostname), `snapshot_url`, `webcam`, `image` (e.g. `1280x720`), `klippy_state`, `moonraker_version` |

`GET /api/status` includes `editable` and `token_required` so a client knows what to expect.
Errors return JSON `{"error": "..."}` with status 400 (bad input), 401 (token), 403 (editing disabled or missing header)
or 404 (unknown printer).
