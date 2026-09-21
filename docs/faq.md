# FAQ

**Does it work while a printer is printing?**
No. Scans are ignored while the printer's state is `printing`, so usage from a running job can't be charged to the wrong
spool. It works when the printer is idle, paused (including a colour change), complete, cancelled or in error. See
[operation.md](operation.md).

**Can I change spools during a colour change?**
That is the intent: during an `M600`/`PAUSE` the printer is `paused`, and scans are accepted. Scan the new spool, then resume.
It's covered by automated tests but hasn't yet been verified on real hardware. Details and caveats in
[operation.md](operation.md#changing-filament-during-a-print-colour-change).

**Do I need to add the beep macros to my printers?**
No. The scanner works exactly the same without them. They only give you a beep or a light at the printer, for when you can't see the
Mainsail/Fluidd screen from where you're standing. Every scan is already confirmed with a console message. See [feedback.md](feedback.md).

**Do I need special hardware?**
No. It uses the webcam each printer already has in Moonraker, and Spoolman's own QR labels. There is no NFC reader, no
tags and no changes to the printers.

**Where does it run?**
On any always-on machine that can reach your printers over the network: a Raspberry Pi, a VM, or the container that
already runs Spoolman. It is not installed on the printers.

**Does it need Spoolman's address?**
No. It talks to each printer's Moonraker, which talks to Spoolman.

**What does it change in Spoolman?**
Only a spool's *Location*. It never deletes or archives spools and never edits weights or filament data.

**What does it change on the printers?**
Only the active spool (through Moonraker), plus console messages, optional pop-ups, and macro calls you configure, and only
when the printer is not printing. It never pauses, resumes or cancels a print.

**Does it send anything to the internet or store camera pictures?**
No. Snapshots are decoded in memory and thrown away; all traffic stays on your network.

**Which QR codes does it understand?**
Spoolman's spool labels (`web+spoolman:s-<id>`), `SM:SPOOL=<id>`, and URLs ending in `/spool/show/<id>`, plus its own
"clear spool" label. See [labels.md](labels.md).

**The QR code is never read.**
Move the label closer (15-30 cm), add light, avoid glare, and use a larger QR. Test a saved snapshot with
`--decode-file`. See [troubleshooting.md](troubleshooting.md).

**Can two printers have the same spool?**
Not by default: when a printer takes a spool, the printer that had it lets go (unless that printer is printing). Turn
that off with `"exclusive": false`.

**Does it support multi-extruder or toolchanger printers?**
Not specially. It sets Moonraker's single active spool for the printer; multi-tool setups are handled by their own macros.

**Is the web page safe to expose?**
It is meant for a trusted home network. The page and its status endpoints are read-only and have no login. Printer editing
is off unless you enable it (with an optional password). Never expose the port to the internet. See [managing-printers.md](managing-printers.md#security).

**How do I stop or uninstall it?**
`systemctl disable --now spool-cam-scanner`, delete the service file and the folder. Nothing was installed on the printers.

**Is this affiliated with Spoolman, Klipper, Moonraker, Mainsail or Fluidd?**
No. It is an independent community project that works with them.
