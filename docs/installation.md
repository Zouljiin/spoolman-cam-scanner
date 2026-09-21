# Installation

The scanner runs on **one always-on machine** that can reach all your printers over the network.
A Raspberry Pi, a VM, or the container that already runs Spoolman all work. Nothing is installed on the printers.

It is light: with motion gating it used about 8% of one CPU core for four idle printers in testing, and it
fits comfortably in a small container (1 CPU, 1 GB RAM). The Python packages need roughly 300 MB of disk.

## Quick way: the installer

On Debian, Ubuntu, Raspberry Pi OS or a Proxmox container, one command does steps 1-5 below:

```bash
curl -fsSL https://raw.githubusercontent.com/Zouljiin/spoolman-cam-scanner/main/install.sh | sudo bash
```

(No `sudo`, or no `curl`? As root, run `apt install -y curl sudo` first, or leave `sudo` out and use `| bash`.)

It checks for Python 3.9+, creates a virtual environment, installs the packages, asks for your first printer's name and
address (writing `config.json` for you), creates a `spoolscan` service user, installs the systemd service and offers to start
it. Nothing is installed on your printers. Other commands and options:

```bash
sudo bash /opt/spoolman-cam-scanner/install.sh update       # newer version, keeps your config.json
sudo bash /opt/spoolman-cam-scanner/install.sh uninstall    # keeps config.json; add --purge to delete everything
sudo bash install.sh --help                                 # --dir, --ref (branch or tag), --no-service, --yes ...
```

The script is short and readable: read it before running it if you like (`curl -fsSLO <the same URL>`, then `less install.sh`).
It has been tested by automated runs on Ubuntu 24.04, using a stand-in for systemd. It has not yet been run on a real Raspberry Pi or Proxmox
container, so please report problems. Prefer to do it yourself? The manual steps follow.

## Manual install

### 1. Install

```bash
apt update && apt install -y python3-venv python3-pip git      # Debian/Ubuntu
git clone https://github.com/Zouljiin/spoolman-cam-scanner.git /opt/spoolman-cam-scanner
cd /opt/spoolman-cam-scanner
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Optional, for better reading of blurry or small codes:

```bash
apt install -y libzbar0 && venv/bin/pip install pyzbar
```

### 2. Configure

```bash
cp config.example.json config.json
nano config.json
```

At minimum, list your printers. Use each printer's address and Moonraker port (usually 7125):

```json
{
  "storage_location": "Shelf",
  "printers": [
    { "name": "Printer 1", "moonraker": "http://192.168.1.101:7125" }
  ]
}
```

All settings are described in [configuration.md](configuration.md).

### 3. Allow the scanner machine in Moonraker

Moonraker only accepts requests from addresses in `trusted_clients` (in each printer's `moonraker.conf`).
The default private-network entries usually already cover it. Check that the scanner machine can reach the printer:

```bash
curl http://192.168.1.101:7125/server/info
```

You should get a block of JSON starting with `{"result"`. Also confirm the printer's Moonraker lists the
`spoolman` and `webcam` components. If you edit `moonraker.conf`, restart Moonraker.

### 4. Test with a dry run

```bash
venv/bin/python spool_cam_scanner.py -c config.json --dry-run
```

You should see one `watching ...` and one `using camera snapshot ...` line per printer. Hold a Spoolman label
in front of a camera; a `setting active spool -> #<id> ...` line appears. Dry-run changes nothing.

If the QR isn't read, save a snapshot from the camera and test it offline:

```bash
venv/bin/python spool_cam_scanner.py --decode-file snapshot.jpg
```

### 5. Run for real, then as a service

Press Ctrl+C, then run without `--dry-run`:

```bash
venv/bin/python spool_cam_scanner.py -c config.json
```

Once you are happy, start it automatically at boot:

```bash
cp systemd/spool-cam-scanner.service /etc/systemd/system/
nano /etc/systemd/system/spool-cam-scanner.service     # adjust paths / add User= if needed
systemctl daemon-reload
systemctl enable --now spool-cam-scanner
journalctl -u spool-cam-scanner -f                     # watch the log
```

## Optional extras

- **Beeps on the printer** (optional; the scanner works the same without them, they just let you hear a scan when you can't see the screen): needs a few macros added to each printer's `printer.cfg`, see [feedback.md](feedback.md#klipper-macros-beeps--leds)
- **Live log web page / dashboards:** [dashboards.md](dashboards.md)

## Updating

With the installer: `sudo bash /opt/spoolman-cam-scanner/install.sh update` (your `config.json` is kept; local edits to program files are replaced).
By hand:

```bash
cd /opt/spoolman-cam-scanner && git pull
venv/bin/pip install -r requirements.txt
systemctl restart spool-cam-scanner
```

If you installed by pasting the script instead of using git, replace `spool_cam_scanner.py` with the new
version and restart the service. Your `config.json` is never overwritten.
