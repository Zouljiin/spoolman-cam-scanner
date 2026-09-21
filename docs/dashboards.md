# Live log page and dashboards

The scanner can serve a small web page with a live log and a status card per printer (camera up/down, print state,
active spool, last scan). It is off by default; turn it on in `config.json`:

```json
"web": { "enabled": true, "port": 8090 }
```

Restart the scanner and open `http://<scanner-machine>:8090/`. The log shows `web page listening on ...` when it is up.

> **Security:** the page and its read-only JSON endpoints have **no authentication** and send `Access-Control-Allow-Origin: *`.
> They show only printer names, spool numbers and scan messages, but keep them on your LAN.
> Set `"host": "127.0.0.1"` to make the page reachable only from the scanner machine itself (for example
> behind your own reverse proxy). Adding/removing printers from the page is a separate, token-protected feature that is off by
> default: see [managing-printers.md](managing-printers.md).

## What the page shows

- **Top bar:** the green **live** light is the connection between your browser and the scanner (it turns red and says
  *disconnected* if the scanner stops or restarts, and reconnects by itself). It says nothing about the printers.
  **Long text: Wrap / Scroll** switches how long lines on the cards are shown: wrapped over several lines, or one line that
  slides back and forth. The **levels** menu can hide warnings. **Pause** freezes the log; **Clear view** empties the
  on-screen log (not the scanner's memory).
- **Printer cards:** the coloured dot is the **camera**: green = the last snapshot worked, red = it failed (hover for the
  reason), grey = not fetched yet. *Printer Status* is Klipper's own state (standby, printing, paused, complete, cancelled,
  error). Scans are ignored while it says *printing*. The printer's name is shown in its **log color** (set per printer in Manage printers, otherwise chosen automatically) so cards and log lines match up. The **Hide / Show** button in the corner hides or shows that
  printer's lines in the log below (the card itself stays).
- **Remembered:** the levels menu, Long text setting and each printer's Hide/Show are saved in your browser (per browser
  and device, so a dashboard iframe and a normal tab don't share them) and survive a refresh. Pause is not saved.
  URL options below override the saved settings for that visit.

## Page options (URL parameters)

| Parameter | Values | Effect |
|---|---|---|
| `view` | `log`, `status` | Show only the log (no header or cards), or only the printer cards. Default: both. |
| `theme` | `dark`, `light` | Force a theme. Default: follows the browser/OS. |
| `printer` | a printer name | Show only that printer's lines in the log (not saved). |
| `hide` | printer names, comma-separated | Start with those printers' lines hidden. |
| `text` | `wrap`, `scroll` | Long-text mode for the cards. |
| `level` | `nowarn`, `WARNING` | Start with warnings hidden (`nowarn`), or with only warnings and errors (`WARNING`). Also selectable in the page. |
| `lines` | number (default `300`) | Lines kept in the on-screen log. |

Example: `http://192.168.1.10:8090/?view=log&theme=dark`

## JSON endpoints

| Endpoint | Returns |
|---|---|
| `/api/status` | Version, uptime and one object per printer (`name`, `camera`, `error`, `print_state`, `spool`, `spool_desc`, `location`, `last_event`). |
| `/api/summary` | Flat values for one-tile widgets: `printers_total`, `printers_online`, `printers_with_spool`, `last_event`, `uptime_seconds`, `version`. |
| `/api/log?after=<id>` | Log lines with id greater than `after`, plus `last_id`. |
| `/health` | `ok` |

## Adding it to a dashboard

Ready-to-paste snippets are in [`integrations/`](../integrations). Replace `SCANNER_IP` with the scanner machine's address.

### Any dashboard with an iframe / website widget

```html
<iframe src="http://SCANNER_IP:8090/?view=log&theme=dark" width="100%" height="400" style="border:0"></iframe>
```

Use the same URL in the dashboard's iframe or "embed website" widget. The page sends no `X-Frame-Options`
header, so it can be framed.

### Homepage (gethomepage)

Homepage's [Custom API widget](https://gethomepage.dev/widgets/services/customapi/) reads the JSON endpoints.
See [`integrations/homepage/services.yaml`](../integrations/homepage/services.yaml). Nested values use dot notation,
e.g. `printers.0.spool` for the first printer's spool, in the order printers appear in `config.json`.

### Dashy

Dashy has an iframe widget. See [`integrations/dashy/conf.yml`](../integrations/dashy/conf.yml) and Dashy's widget
documentation for the full list of iframe options.

### Home Assistant

A Webpage (`iframe`) card: [`integrations/home-assistant/card.yaml`](../integrations/home-assistant/card.yaml).

### Not possible

Mainsail, Fluidd and Spoolman itself have no documented way to add custom panels, so the page can't be embedded
inside them. Open it in its own tab, or use one of the dashboards above.

## If the embedded page is blank

- **HTTPS dashboard + HTTP page:** browsers block an `http://` iframe inside an `https://` page (mixed content). Either
  serve the dashboard over HTTP, or put the scanner page behind a reverse proxy with HTTPS.
- **Reverse proxy at a sub-path** (e.g. `/scanner/`): the page uses relative URLs, so it works as long as you open it with
  the trailing slash. (Not yet tested.)
- The dashboard's browser must be able to reach the scanner machine's port directly.
