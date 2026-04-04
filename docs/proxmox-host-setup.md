# Proxmox Host Setup

This MVP supports one deployment path: direct installation onto a Proxmox VE host that has a monitor attached locally.

## Prerequisites

- Proxmox VE host with `systemd`
- root shell access
- A target VM that exposes a SPICE display
- Package sources that can install:
  - `python3`
  - `python3-evdev`
  - `cage`
  - `seatd`
  - `virt-viewer`
  - `wlopm`

## Install

1. Clone or copy this repository onto the Proxmox host.
2. Run `sudo ./install.sh`.
3. Edit `/etc/relayinner-display/config.toml` and set at least:
   - `[target].vmid`
   - `[target].node_name`
   - `[display].output_name` if you want to pin a specific connector name
   - `[input].power_button_event` if the default evdev path does not match the host
4. Start the services after editing the config:

```sh
systemctl restart relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
```

5. Reboot once to confirm that `tty1` returns directly into the kiosk session.

Installer flags:

- `./install.sh --skip-package-install` skips the `apt-get` step when the required packages are already present.
- `./install.sh --replace-config` backs up the existing `/etc/relayinner-display/config.toml` and replaces it with the sample config.

## Managed Paths

- `/usr/local/lib/relayinner-display/`
- `/usr/local/share/relayinner-display/`
- `/etc/relayinner-display/config.toml`
- `/etc/systemd/system/relayinner-display-seatd.service`
- `/etc/systemd/system/relayinner-display-kiosk.service`
- `/etc/systemd/system/relayinner-displayd.service`
- `/etc/systemd/logind.conf.d/relayinner-display.conf`
- `/run/relayinner-display/`

## Managed Services

- `relayinner-display-seatd.service`
- `relayinner-display-kiosk.service`
- `relayinner-displayd.service`

The installer also masks `getty@tty1.service` so the kiosk stack owns `/dev/tty1`. If `display-manager.service` exists, the installer disables and masks it as well.

## Verification

Use these checks after installation:

```sh
systemctl status relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
journalctl -u relayinner-displayd.service -u relayinner-display-kiosk.service -b
cat /run/relayinner-display/daemon.state.json
```

If power-button forwarding is enabled, confirm the logind override is active:

```sh
grep -R HandlePowerKey /etc/systemd/logind.conf /etc/systemd/logind.conf.d /run/systemd/logind.conf.d /usr/lib/systemd/logind.conf.d 2>/dev/null
```
