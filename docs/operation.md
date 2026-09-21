# How it operates

This page describes exactly what the scanner does and, just as important, **when it does nothing**.

## The short version

1. It watches each printer's webcam (a still snapshot about twice a second) for a Spoolman QR label.
2. When it sees a spool label, it checks the printer's state **at that moment**.
3. If the printer is **not printing**, it sets the scanned spool as that printer's active spool and updates the
   spool's location in Spoolman.
4. If the printer **is printing**, it ignores the scan and touches nothing.

## When scans are accepted, and when they are not

The scanner reads Klipper's own job state (`print_stats.state`) from Moonraker. Only one state blocks scans.

| Klipper state | What it means | Scan |
|---|---|---|
| `standby` | Idle, no job | **accepted** |
| `printing` | A job is running. This includes the heat-up and any start G-code, from the moment the job starts | **ignored** |
| `paused` | The job is paused: Mainsail/Fluidd **Pause**, a filament-runout pause, or a **colour change** (`M600` implemented with a macro that calls `PAUSE`) | **accepted** |
| `complete` | The last job finished | **accepted** |
| `cancelled` | The last job was cancelled | **accepted** |
| `error` | Klipper is in an error state | **accepted** |
| unknown | Klipper is not ready, or the state can't be read. Moonraker can still set the spool | **accepted** |

The rule can be switched off with `"skip_while_printing": false`. That is **not recommended**: a spool swapped by
mistake mid-print would send the rest of the job's filament usage to the wrong spool.

### Changing filament during a print (colour change)

A colour change works because Klipper reports `paused` while the job is paused, and the scanner accepts scans in that
state. The intended sequence:

1. Your G-code triggers a pause (for example `M600` handled by a macro that calls `PAUSE`, or you press **Pause**).
2. Klipper's state becomes `paused`. The scanner is now willing to accept a scan.
3. You swap the spool and hold the new spool's label up to the camera. The scanner sets it as the active spool, and
   the console message (and optional pop-up/beeps) confirms it.
4. You press **Resume**. The state returns to `printing` and scans are ignored again.

Things to know:

- **Scan before you resume.** After `RESUME` the printer is `printing` and scans are ignored.
- **Take the label out of view and show it again.** A label that stays in view continuously is treated as the same scan
  (see `cooldown`), so if you were already holding it up while the printer was still `printing`, remove it for a few
  seconds and show it again once the printer is paused.
- **Klipper has no built-in `M600`.** Your printer configuration must provide the macro that calls `PAUSE`. The scanner
  does not pause, resume, or cancel anything itself, and it does not detect filament runout.
- **Feedback runs during a pause.** While `paused`, the console message, pop-up and your beep/LED macros are allowed. Keep those
  macros to beeps and LEDs: **never put movement in them**, because the head is parked over the print.
- **Usage accounting is Moonraker's job.** Moonraker reports filament usage to Spoolman at its `sync_rate` and keeps
  pending usage per spool id, so usage after the swap should go to the new spool. See the Moonraker and Spoolman docs for the
  exact accounting at the moment of the switch.
- **Status of this feature:** the pause/colour-change behaviour is covered by automated tests against simulated printers.
  It has **not yet been verified during a real colour change on real hardware**. Please report your results.

## What a scan does

```
same spool still in view (seen again within `cooldown`)?  -> ignore (it's the same scan)
printer is printing?                                      -> ignore, nothing is sent to the printer
clear label?                                              -> clear the active spool, set its location to storage_location
spool already active?                                     -> confirm "already active", change nothing
spool not in Spoolman?                                    -> report "not found", change nothing
otherwise:
    set the spool as this printer's active spool
    set the spool's Spoolman location to the printer's name
    if a different spool was active: put that one back to storage_location
    confirm (console / pop-up / macro / command)
    if `exclusive`: any other printer that still lists this spool releases it
                    (never a printer that is printing)
```

## Locations in Spoolman

| Event | Location change |
|---|---|
| Spool assigned to a printer | The spool's location becomes the printer's name (or its `location` setting). Can be turned off with `set_location`. |
| Another spool replaces it on the same printer | The replaced spool goes to `storage_location` (if you set one). |
| Spool is taken by another printer | Its location becomes the new printer; the old printer's active spool is cleared. |
| Clear label scanned | The cleared spool goes to `storage_location` (if you set one). |

If you don't set a `storage_location`, spools that leave a printer keep whatever location they had.

## Moving a spool between printers

Show the label to the new printer. It becomes the active spool there, and the printer that had it is told to let go
(shown in that printer's console as "Spool #N removed (moved to ...)"). A printer that is **printing** is never
touched, so it keeps the spool until its job is over.

## The clear label

A special QR label (`SM:CLEAR`, ready to print in `labels/clear-spool-label.png`). Showing it to a printer that is not
printing un-assigns that printer's active spool.

## Feedback

Every accepted, rejected or ignored scan is confirmed in the printer's console. Optionally also a pop-up dialog, beeps
or LEDs (via Klipper macros), and a command on the scanner machine. While a printer is printing, only the command on the
scanner machine runs. See [feedback.md](feedback.md).

## What the scanner never does

- It never pauses, resumes, starts or cancels a print, and never changes G-code files.
- It never sends anything to a printer that is printing (except the optional command on the scanner machine, which
  runs on the scanner machine, not on the printer).
- It never deletes or archives spools and never edits filament data or weights in Spoolman.
- It never stores camera pictures: each snapshot is decoded in memory and discarded.
- It never talks to the internet: all traffic stays on your network.

The only things it writes are the active spool (through Moonraker), a spool's Location in Spoolman, console/pop-up/macro
commands to printers that are not printing, and its own `config.json` when you edit printers on the web page.

## When things go wrong

| Situation | What happens |
|---|---|
| A printer or its Moonraker is unreachable | A warning is logged (at most once a minute) and the scanner retries every 10 seconds. Other printers keep working. |
| A camera doesn't answer | Same, and the printer's status dot on the web page turns red with the reason on hover. |
| Klipper is not ready | The print state is unknown, so scans are accepted; Moonraker can still record the spool. Console messages may not be delivered. |
| Spoolman can't be reached | The spool lookup is skipped (a warning is logged) and the spool is still set through Moonraker. |
| The label isn't a known spool | "Spool #N not found in Spoolman" is reported and nothing changes. |
| Something else (a website QR, a product barcode) is in view | Ignored. |
| The scanner restarts | Nothing is lost: active spools live in Moonraker, locations in Spoolman. |

## Resource use

A snapshot is fetched every `poll_interval` seconds (every `busy_poll_interval` while printing). QR decoding, the
expensive part, runs only while the picture is changing (`motion_gate`). With four idle printers it used about 8% of one
CPU core in testing and about 125 MB of memory.
