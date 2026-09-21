# Configuration

Settings live in `config.json` (JSON, so no comments). Copy [`config.example.json`](../config.example.json) to start.
Every setting except `printers` is optional.

## Global settings

| Setting | Default | Meaning |
|---|---|---|
| `poll_interval` | `0.5` | Seconds between camera snapshots per printer. |
| `cooldown` | `10` | Seconds a spool must be out of view before showing it again counts as a new scan. |
| `skip_while_printing` | `true` | Ignore scans on a printer that is printing. **Leave on** unless you know why you need it off. |
| `exclusive` | `true` | When a spool is assigned to a printer, take it off any other printer that still lists it (never one that is printing). |
| `set_location` | `true` | Set the spool's Spoolman *Location* to the printer's name when assigned. |
| `storage_location` | none | Location given to a spool when it is cleared or replaced (e.g. `"Shelf"`). Unset = leave the location alone. |
| `motion_gate` | `true` | Only decode QR codes while the picture is changing (saves CPU). |
| `motion_threshold` | `1.5` | How much the picture must change to count as motion. Raise it (try `3`) if a noisy camera never goes idle. |
| `motion_hold` | `3.0` | Seconds to keep decoding after motion stops, so a label held still is still read. |
| `busy_poll_interval` | `2.0` | Seconds between snapshots while a printer is printing (scans are ignored then anyway). |
| `feedback` | none | See [feedback.md](feedback.md). |
| `web` | disabled | See below and [dashboards.md](dashboards.md). |

## `printers` (required)

A list; one entry per printer.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Shown in logs, messages, and used as the spool's Location. |
| `moonraker` | yes | Base URL of Moonraker, e.g. `http://192.168.1.101:7125`. |
| `snapshot_url` | no | Camera snapshot URL. Normally auto-discovered from Moonraker; set it if discovery fails. |
| `webcam` | no | Name of the Moonraker webcam to use when a printer has several. |
| `location` | no | Spoolman Location to use instead of `name`. |
| `color` | no | Color of this printer's name in the web page's log and cards, as `#rrggbb`. Unset = a distinct color is chosen automatically. Easiest to set from the web page's edit form. |
| `api_key` | no | Sent as `X-Api-Key` if your Moonraker requires authorization. (Untested.) |
| `feedback` | no | Per-printer override of the global `feedback` section. |

## `web`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the live log / status page on. |
| `host` | `"0.0.0.0"` | Address to listen on. Use `"127.0.0.1"` to allow only the local machine. |
| `port` | `8090` | Port. |
| `max_lines` | `1000` | Log lines kept in memory for the page. |
| `allow_edit` | `false` | Allow adding/editing/removing printers from the page. See [managing-printers.md](managing-printers.md). |
| `edit_token` | none | Optional password for the edit functions (recommended if others can reach the page). |

The page itself has **no authentication** (it is read-only); keep it on a trusted network.

## `feedback`

| Key | Default | Meaning |
|---|---|---|
| `popup` | `false` | Show a small dialog in Mainsail/Fluidd for each scan. |
| `popup_seconds` | `4` | Auto-close the dialog after this many seconds (`0` = stays until dismissed). |
| `gcode` | none | Map of event name to a Klipper macro to run on the printer. |
| `command` | none | Command to run on the scanner machine for each event. |

Events: `assigned`, `already_active`, `cleared`, `nothing_to_clear`, `removed`, `not_found`, `ignored`.

`config.example.json` contains the beep macros switched off, under the key `_gcode` (keys starting with `_` are ignored). Once the macros are installed on
your printers (see [feedback.md](feedback.md)), rename `_gcode` to `gcode`. To turn them off for one printer, untick **Beep macros** in the web page's
Manage printers form, or give that printer a `feedback` with empty macro names.
Details in [feedback.md](feedback.md).

## Command-line options

```
-c, --config FILE     config file (default config.json)
--dry-run             log what would happen; change nothing
--decode-file IMAGE   decode a saved snapshot and exit (for testing)
-v, --verbose         debug logging
```
