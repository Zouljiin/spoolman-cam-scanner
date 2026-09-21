# Troubleshooting

Run with `-v` for debug logging, and use `--dry-run` to see decisions without changing anything.

| Symptom | Likely cause / fix |
|---|---|
| `no enabled webcam with a snapshot_url in Moonraker` | The printer has no webcam defined in Moonraker (or it's disabled). Add it in Mainsail/Fluidd settings, or set `"snapshot_url"` in `config.json`. |
| `ReadTimeout` on the camera | The camera service is stuck or stopped. Test: `curl -m 10 -o /tmp/x.jpg "http://<printer>/webcam/?action=snapshot"`. If the camera doesn't show in Mainsail either, fix that first (restart the camera service, check the USB camera). |
| `Connection refused` / timeout to port 7125 | Wrong address, Moonraker down, or the scanner machine isn't in `trusted_clients` in `moonraker.conf`. |
| Camera found but QR never read | Save a snapshot and run `--decode-file snapshot.jpg`. Move the label closer, add light, avoid glare. See [labels.md](labels.md). |
| QR read but "not a Spoolman label" | Run `--decode-file` to see the content; see supported formats in [labels.md](labels.md). |
| `spool N is not in Spoolman` | The label refers to a spool id that doesn't exist (deleted or from another Spoolman). |
| Nothing happens while a print is running | By design: scans are ignored during a print. |
| Messages don't appear in the console | Needs `[respond]` in Klipper (included by default in `mainsail.cfg`). Test: `curl -X POST http://<printer>:7125/printer/gcode/script -H "Content-Type: application/json" -d '{"script":"RESPOND MSG=\"hello\""}'`. |
| `Unknown command: SPOOL_SCAN_OK` | The beep macros aren't installed on that printer. Install them (`klipper/spool_feedback.cfg` plus `[include spool_feedback.cfg]` in `printer.cfg`), or switch the macros off for that printer; both are explained in [feedback.md](feedback.md#klipper-macros-beeps--leds). |
| Macro runs but no sound | The printer has no `M300` macro or buzzer pin. Type `M300` in the console to test. |
| Spool clears fail with HTTP 400 | Old version: update to 0.1.0+ (uses an empty body instead of `null`). |
| High CPU | Raise `poll_interval`, raise `motion_threshold` (noisy camera), or check the camera isn't returning very large images. |
| Web page not reachable | `"web": {"enabled": true}` set? Look for `web page listening` in the log; check the port isn't already in use and no firewall blocks it. |
| Embedded page blank in a dashboard | See [dashboards.md](dashboards.md#if-the-embedded-page-is-blank) (usually HTTPS/HTTP mixed content). |

## Compatibility reports

| Printer / camera | Result |
|---|---|
| Several Klipper printers (models not recorded), Moonraker v0.11.0, Mainsail, camera snapshot at `/webcam/?action=snapshot` | Works: assign, clear, locations, feedback. |
| Elegoo Neptune 4 Max (Mainsail added to stock Klipper), webcam at `/webcam/?action=snapshot` | The camera was found, but snapshots timed out and the webcam didn't display in Mainsail either, so that was a camera-service problem rather than a scanner one. Current status unconfirmed. |

Add your own row via a pull request or issue.
