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

Successful installer runs also rewrite `/var/lib/relayinner-display/install-state.json`. Re-running without `--replace-config` preserves the current config and records `config_state.action=preserved`; re-running with `--replace-config` records `config_state.action=replaced` plus the backup path created during that run.

## Uninstall

Run uninstall from the repository checkout on the Proxmox host:

```sh
sudo ./uninstall.sh
```

Default uninstall performs these steps conservatively:

- stops `relayinner-display-seatd.service`, `relayinner-display-kiosk.service`, and `relayinner-displayd.service`
- disables the relay services so they do not come back on the next boot
- removes `/usr/local/lib/relayinner-display/`, `/usr/local/share/relayinner-display/`, the relay unit files, and `/etc/systemd/logind.conf.d/relayinner-display.conf`
- runs `systemctl daemon-reload`
- restores `getty@tty1.service`
- preserves `/etc/relayinner-display/config.toml`
- deletes `/var/lib/relayinner-display/install-state.json` only after the restore and cleanup steps succeed

If the installer-created service user is still recorded as `service_user.created_by_installer=true`, uninstall also removes the `relayinner-display` user and `/var/lib/relayinner-display/`. If the installer did not create that user, uninstall leaves the user and home directory intact.

Use purge mode only when you want the relay config removed too:

```sh
sudo ./uninstall.sh --purge-config
```

`--purge-config` performs the default uninstall and also removes:

- `/etc/relayinner-display/config.toml`
- `/etc/relayinner-display/config.toml.bak.*`
- `/etc/relayinner-display/` if it becomes empty after the purge

`/var/lib/relayinner-display/install-state.json` is the restore authority for uninstall. When it is present, uninstall uses it to decide:

- whether the relay service user can be removed safely
- whether `display-manager.service` existed and was changed by the installer
- which canonical relay-managed unit and asset paths should be removed

`display-manager.service` is restored only when install-state proves both `existed=true` and `changed_by_installer=true`. If the unit was masked before install, uninstall leaves it masked. If it was enabled before install, uninstall enables it again. If it was active before install, uninstall starts it again.

If `install-state.json` is missing, uninstall prints one best-effort warning, removes the canonical relay assets by known path, restores `getty@tty1.service`, and preserves the relay service user plus home directory because installer ownership can no longer be proven safely. In that mode it does not attempt to restore `display-manager.service`.

After uninstall, verify that the relay stack is gone and the host login path is back:

```sh
systemctl status getty@tty1.service
test -e /etc/systemd/system/relayinner-displayd.service && echo "relay unit still present" || echo "relay units removed"
test -e /etc/systemd/logind.conf.d/relayinner-display.conf && echo "logind override still present" || echo "logind override removed"
test -e /var/lib/relayinner-display/install-state.json && echo "install-state still present" || echo "install-state removed"
```

## Managed Paths

- `/usr/local/lib/relayinner-display/`
- `/usr/local/share/relayinner-display/`
- `/etc/relayinner-display/config.toml`
- `/etc/systemd/system/relayinner-display-seatd.service`
- `/etc/systemd/system/relayinner-display-kiosk.service`
- `/etc/systemd/system/relayinner-displayd.service`
- `/etc/systemd/logind.conf.d/relayinner-display.conf`
- `/var/lib/relayinner-display/install-state.json`
- `/run/relayinner-display/`

## Managed Services

- `relayinner-display-seatd.service`
- `relayinner-display-kiosk.service`
- `relayinner-displayd.service`

The installer also masks `getty@tty1.service` so the kiosk stack owns `/dev/tty1`. If `display-manager.service` exists, the installer disables and masks it as well.

For Spec 15, the rendered units also enforce `StartLimitIntervalSec=120` and `StartLimitBurst=5` so repeated crash loops stop being retried indefinitely.

`/var/lib/relayinner-display/install-state.json` is the authoritative record of installer-managed host state for later uninstall or restore work. It records the managed paths, config preservation or replacement result, whether the service user was created during that run, and the pre-install state of `getty@tty1.service` plus `display-manager.service`.

## Verification

Use these checks after installation:

```sh
systemctl status relayinner-display-seatd.service relayinner-displayd.service relayinner-display-kiosk.service
journalctl -u relayinner-displayd.service -u relayinner-display-kiosk.service -b
sudo cat /var/lib/relayinner-display/install-state.json
stat -c '%a %U %G %n' /var/lib/relayinner-display/install-state.json
cat /run/relayinner-display/daemon.state.json
```

To focus on subsystem-scoped entries from the MVP logging contract, filter the journal output for logger names such as:

```sh
journalctl -u relayinner-displayd.service -u relayinner-display-kiosk.service -b | grep 'relayinner-display\.'
```

The runtime state file now records the public appliance state and key fault markers:

- `appliance_state`
- `session_ready`
- `vm_power_state`
- `display_power_applied`
- `degraded_reason`
- `last_console_exit`

If power-button forwarding is enabled, confirm the logind override is active:

```sh
grep -R HandlePowerKey /etc/systemd/logind.conf /etc/systemd/logind.conf.d /run/systemd/logind.conf.d /usr/lib/systemd/logind.conf.d 2>/dev/null
```

## Troubleshooting

If you need to confirm exactly what the installer changed on the host, inspect `/var/lib/relayinner-display/install-state.json` first. The `config_state` section shows whether the current config was created, preserved, or replaced, and `conflicting_units` captures whether `getty@tty1.service` or `display-manager.service` existed and whether this installer run changed them.

When the appliance is not showing the guest, inspect the state file first:

- `appliance_state=display_sleeping` means the VM has remained off past `dpms_off_delay_ms`.
- `appliance_state=waiting_for_vm` means the session is healthy but the VM is not in a runnable state.
- `appliance_state=degraded` means a local runtime dependency, power-button validation, or repeated Proxmox command failure tripped the Spec 15 failure policy.

Then inspect journald by subsystem:

- `relayinner-display.proxmox` for `qm`/`pvesh` failures and retry exhaustion
- `relayinner-display.console` for `remote-viewer` launch or exit problems
- `relayinner-display.display` for display power helper failures
- `relayinner-display.input` for host power-button validation or evdev read failures
- `relayinner-display.session` for session connection and state-transition events

If the kiosk journal shows the `libseat` sequence `Could not connect to socket /run/seatd.sock: Permission denied`, `Could not open target tty: Permission denied`, `Timeout waiting session to become active`, or `Unable to create the wlroots backend`, confirm the installed kiosk unit launches `seatd-launch -- cage -- /usr/local/lib/relayinner-display/session-entrypoint`. Older installs rendered the kiosk unit differently; rerun `sudo ./install.sh`, then restart `relayinner-display-seatd.service`, `relayinner-displayd.service`, and `relayinner-display-kiosk.service` to refresh the unit files before chasing host-level permissions.
