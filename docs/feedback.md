# Feedback

Every scan is confirmed. There are four independent channels; the first is always on.

**None of this is needed for the scanner to work.** Scans set the active spool, update locations and clear spools whether or not any
pop-up, macro or command is configured. The extra channels only give you feedback where you are: a beep or light at the printer for when you
can't see the Mainsail/Fluidd screen from where you're standing, or a message on your own machine.

| Channel | Where you see it | Enable with |
|---|---|---|
| Console message | Mainsail/Fluidd console on that printer | always on |
| Pop-up dialog | Mainsail/Fluidd (and KlipperScreen, unconfirmed) | `"popup": true` |
| Klipper macro | Beeps/LEDs on the printer | `"gcode": {...}` |
| Host command | Anything on the scanner machine | `"command": "..."` |

## Events

| Event | When | Example console message |
|---|---|---|
| `assigned` | Spool set on this printer | `Active spool set to #41 GST3D Blue PLA+` (adds `- spool #7 removed` if it replaced one) |
| `already_active` | The scanned spool is already active | `Spool #41 is already active` |
| `removed` | Another printer claimed this printer's spool | `Spool #41 removed (moved to Printer 2)` |
| `cleared` | Clear label scanned | `Spool #41 removed (clear label scanned)` |
| `nothing_to_clear` | Clear label scanned but no spool was active | `No active spool to clear` |
| `not_found` | Valid label, but the spool isn't in Spoolman | `Spool #999 not found in Spoolman` |
| `ignored` | Scan while the printer is printing | (host command only, see below) |

`already_active` uses the `assigned` macro and `removed`/`nothing_to_clear` use the `cleared` macro, unless you
give them their own entries.

## Never during a print

While a printer is printing, **nothing** is sent to it: no console message, pop-up or macro. Only the host
`command` runs (with event `ignored`), so you can make a "rejected" sound on the scanner machine without touching the job.

## Klipper macros (beeps / LEDs)

Beeps are **optional**: the scanner works exactly the same without them, and they are only there so you can hear that a scan was read when you can't see
the screen. They need a few macros installed **on each printer that should beep**. The scanner only calls them; it can't install them for you.

### 1. Put the macros on the printer

1. Copy [`klipper/spool_feedback.cfg`](../klipper/spool_feedback.cfg) into the printer's config folder (the one containing
   `printer.cfg`). In Mainsail or Fluidd, use the configuration/machine page: upload the file, or create a new file named
   `spool_feedback.cfg` and paste the contents in.
2. Open `printer.cfg` and add this line (anywhere near the other `[include ...]` lines):

   ```
   [include spool_feedback.cfg]
   ```

3. Click **Save & Restart** (or restart Klipper).

The file defines three macros: `SPOOL_SCAN_OK` (two quick blips: spool assigned), `SPOOL_SCAN_CLEARED` (one longer beep: spool
removed) and `SPOOL_SCAN_ERROR` (three fast low beeps: spool not in Spoolman). The sounds differ by rhythm, not only pitch, so
they stay distinguishable on buzzers that play every pitch the same.

### 2. Make sure the printer can beep

The macros use `M300`. Type `M300` in the printer's console: if it answers with a beep (or no error), you're set. If it says
`Unknown command`, the printer has no `M300` macro; the bottom of `spool_feedback.cfg` has a commented-out example (you must set your
own board's buzzer pin). No buzzer at all? Delete the `M300` lines from the macros and use `SET_LED` for a light instead, or leave
the macros out. Test the macros with the buttons in the **Macros** panel before continuing.

### 3. Tell the scanner to use them

In `config.json`, in the `feedback` section:

```json
"feedback": {
  "gcode": { "assigned": "SPOOL_SCAN_OK", "cleared": "SPOOL_SCAN_CLEARED", "not_found": "SPOOL_SCAN_ERROR" }
}
```

Then restart the scanner (`sudo systemctl restart spool-cam-scanner`). The scanner runs the macro named for the event and passes
`SPOOL=<id>`.

### Printers without the macros

If a printer doesn't have the macros, its console shows `Unknown command: SPOOL_SCAN_OK` on every scan (nothing breaks). Turn the macros off
for that printer only. The easy way: on the live page, **Manage printers**, **Edit** the printer, and untick **Run the beep macros on this
printer** (see [managing-printers.md](managing-printers.md)). By hand, the same thing is a per-printer override in `config.json` that sets the
events to empty:

```json
{ "name": "Printer 2", "moonraker": "http://192.168.1.102:7125",
  "feedback": { "gcode": { "assigned": "", "cleared": "", "not_found": "" } } }
```

The console message and pop-up still work for that printer.

### Finding your beeper pin

The macros play tones with `M300`, which drives a buzzer on one of your board's pins, so you need that pin's name. First check whether you
already have one: search your `printer.cfg` and the files it includes for `M300`, `beeper` or `buzzer`, or just type `M300` in the console.
If it works, you're done.

Known examples. **Pins vary by board and revision, so always test before relying on one:**

| Printer / board | Beeper pin | Where that comes from |
|---|---|---|
| Elegoo Neptune 4 and 4 Pro | `PA2` | A community macro collection for these printers, [Molodos/klipper-macros](https://github.com/Molodos/klipper-macros). Elegoo's stock config header says the Neptune 4, 4 Pro, 4 Plus and 4 Max share one mainboard, so the Plus and Max probably use the same pin (not confirmed) |
| Creality CR-30 | `PC6` | Klipper's example config [`printer-creality-cr30-2021.cfg`](https://github.com/Klipper3d/klipper/blob/master/config/printer-creality-cr30-2021.cfg) |
| Creality v4.2.10 mainboard | `PC6` | Klipper's example [`generic-creality-v4.2.10.cfg`](https://github.com/Klipper3d/klipper/blob/master/config/generic-creality-v4.2.10.cfg) (shown commented out) |
| Ender 3 / CR-10 style with the stock 12864 LCD (ribbon from the display's EXP3 plug to the board's EXP1) | `EXP1_1` | Klipper's [`sample-lcd.cfg`](https://github.com/Klipper3d/klipper/blob/master/config/sample-lcd.cfg); it needs `[board_pins]` aliases for the EXP plugs, as that file explains |
| RepRapDiscount 128x64 and 2004 Smart Controller displays | `EXP1_1` | Klipper's [`sample-lcd.cfg`](https://github.com/Klipper3d/klipper/blob/master/config/sample-lcd.cfg) |

Many printers have **no buzzer Klipper can use**, for example ones whose stock screen is a serial touch display that Klipper doesn't drive.
Then skip the beeps: the console message and pop-up still confirm every scan.

If yours isn't listed:

1. Look in your printer maker's own Klipper config (if they publish one) for `beeper`, `buzzer` or `M300`.
2. Look in Klipper's [example configs](https://github.com/Klipper3d/klipper/tree/master/config) for your board (`generic-<board>.cfg`) or printer and
   search for `beeper`. Some list it commented out.
3. Check your board's documentation or silkscreen for `BEEPER`, `BUZZER` or `BZ`. It is normally on the LCD connector, so it only exists if
   an LCD with a buzzer is connected.
4. Marlin's pin file for your board defines `BEEPER_PIN`, which is a useful hint (Marlin's pin names may need translating to Klipper's).

Then test it safely. Add the candidate pin to `printer.cfg` (the `M300` macro in `spool_feedback.cfg` expects the name `beeper`):

```
[output_pin beeper]
pin: PA2          # your pin here
pwm: True
value: 0
shutdown_value: 0
```

After **Save & Restart**, run `SET_PIN PIN=beeper VALUE=0.5 CYCLE_TIME=0.001` in the console: you should hear a tone. Stop it with
`SET_PIN PIN=beeper VALUE=0`.

**Don't guess a pin.** A pin that already drives a heater, fan or motor could switch it. Only use a pin from one of the sources above, and search
your `printer.cfg` for it first: Klipper refuses to start if a pin is claimed twice, which means it isn't free. If in doubt, skip the beeps.

### Keep the macros harmless

While a print is **paused** (for example during a colour change) the macros do run, with the head parked over the print. Keep them to beeps
and LEDs: **never add movement** (G0, G1, G28, ...) or heater/fan changes to them. They are never run while a printer is printing.
The same warning is at the top of `spool_feedback.cfg`.

## Host command

```json
"feedback": { "command": "/opt/spoolman-cam-scanner/feedback.sh" }
```

Runs on the scanner machine for every event (not through a shell). Details arrive as environment variables:

| Variable | Value |
|---|---|
| `SPOOL_EVENT` | the event name |
| `SPOOL_PRINTER` | printer name |
| `SPOOL_ID` | spool id, or empty |
| `SPOOL_DESC` | e.g. `GST3D Blue PLA+` |
| `SPOOL_MESSAGE` | the console message |

Example `feedback.sh`, speaking through the machine's speaker (requires `espeak`):

```sh
#!/bin/sh
case "$SPOOL_EVENT" in
  assigned) espeak "$SPOOL_PRINTER has spool $SPOOL_ID" ;;
  ignored)  espeak "$SPOOL_PRINTER is busy" ;;
esac
```

Other ideas: toggle a GPIO buzzer or LED, `curl` a smart-home webhook or an ESPHome device.
