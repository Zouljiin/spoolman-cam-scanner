# Labels

## Spool labels

Print labels from Spoolman as usual (its label printing feature puts a QR code on each label).
The scanner understands these QR contents:

| QR content | Where it comes from |
|---|---|
| `web+spoolman:s-123` | Spoolman's default spool label |
| `SM:SPOOL=123` | Community barcode-scanner format |
| `https://<host>/spool/show/123` | Labels whose QR is set to a Spoolman URL |

Filament labels (`web+spoolman:f-...`) are deliberately **not** treated as spools.
If your labels use a different format, run `--decode-file` on a photo of one to see what it contains;
the patterns are near the top of `spool_cam_scanner.py`.

## The "clear spool" label

A special label whose QR contains `SM:CLEAR`. Showing it to a printer's camera un-assigns that printer's active
spool (and sets its location to `storage_location`, if configured). A ready-to-print image is in
[`labels/clear-spool-label.png`](../labels/clear-spool-label.png). To make your own:

```bash
qrencode -s 10 -o clear.png "SM:CLEAR"
```

## Tips for reliable scanning

- Hold the label 15-30 cm from the camera so the QR fills a good part of the frame. Larger QR codes on labels help a lot.
- Many USB cameras are fixed-focus; if it won't read, try a different distance.
- Turn on the chamber light. Matte labels beat glossy ones (no glare).
- Wave it in and hold still for a second or two.
