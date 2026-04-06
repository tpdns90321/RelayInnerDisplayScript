# Proxmox Host Setup

This MVP supports one deployment path: direct installation onto a Proxmox VE host that has a monitor attached locally.

## Prerequisites

- Proxmox VE host with `systemd`
- root shell access
- A target VM that exposes either a SPICE display, an operator-prepared loopback-only VNC endpoint, a fully operator-prepared Looking Glass guest, or a Sunshine host reachable from the Proxmox host for Moonlight
- Package sources that can install:
  - `python3`
  - `python3-evdev`
  - `cage`
  - `seatd`
  - `virt-viewer`
  - `wlr-randr`

When `console_backend = "moonlight"`, operators also need Linux `moonlight-qt` version `6.0.0` or newer. The installer does not add that package for you.

## Install

1. Clone or copy this repository onto the Proxmox host.
2. Run `sudo ./install.sh`.
3. Edit `/etc/relayinner-display/config.toml` and set at least:
   - `[target].vmid`
   - `[target].node_name`
   - `[target].console_backend` if you are switching from the default SPICE path
   - `[console.vnc].display_number` plus matching VM `args: -vnc 127.0.0.1:<display_number>` when using `console_backend = "vnc"`
   - `[console.looking_glass].shm_file` plus any renderer or SPICE overrides when using `console_backend = "looking-glass"`
   - `[console.moonlight].host` plus any non-default `app`, `base_port`, or `state_dir` when using `console_backend = "moonlight"`
   - `[display].output_name` if you want to pin a specific connector name; this is recommended when the default `wlr-randr` helper should target one physical connector only
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

## VNC Backend

Use `console_backend = "vnc"` only after the target VM has been prepared with a loopback-only VNC bind in Proxmox VM config. The supported shape is:

```text
args: -vnc 127.0.0.1:77
```

Then configure the relay to match:

```toml
[target]
console_backend = "vnc"

[console.vnc]
bind_host = "127.0.0.1"
display_number = 77
viewer = "remote-viewer"
```

The relay derives TCP port `5900 + display_number`, launches `remote-viewer --full-screen vnc://<bind_host>:<port>`, and refuses to run if `qm config <vmid>` exposes VNC on any non-loopback address or on a different display number. The installer does not rewrite VM config for you.

## Looking Glass Backend

Use `console_backend = "looking-glass"` only after the guest already satisfies the upstream Looking Glass requirements. This relay backend is intentionally preflight-only support:

- the installer does not create `/dev/kvmfr*`
- the installer does not configure GPU passthrough or VFIO
- the installer does not install the guest-side Looking Glass host application

Configure the relay like this:

```toml
[target]
console_backend = "looking-glass"

[console.looking_glass]
shm_file = "/dev/kvmfr0"
binary = "looking-glass-client"
renderer = "auto"
fullscreen = true
disable_host_screensaver = true
spice_enabled = true
```

At runtime the daemon verifies that the configured client binary exists, that `shm_file` already exists, and that the `relayinner-display` session user can read the file or device before it launches `looking-glass-client` in the kiosk session. If any of those visible host-side prerequisites fail, the appliance moves into controlled `degraded` state instead of showing a blank screen.

For the required host and guest preparation, follow the upstream documentation:

- <https://looking-glass.io/docs/stable/requirements/>
- <https://looking-glass.io/docs/stable/install_client/>

## Moonlight Backend

Use `console_backend = "moonlight"` only after the guest already runs Sunshine and the Proxmox host already has Linux `moonlight-qt` version `6.0.0` or newer. This Spec 30 slice intentionally covers only the backend contract, managed workspace, and direct `moonlight stream` launch:

- the installer does not configure Sunshine inside the guest
- the installer does not automate Moonlight pairing
- this path targets Linux `moonlight-qt`, not mobile clients or browser flows

Configure the relay like this:

```toml
[target]
console_backend = "moonlight"

[console.moonlight]
binary = "moonlight"
host = "192.168.50.20"
base_port = 47989
app = "Desktop"
state_dir = "/var/lib/relayinner-display/moonlight"
quit_app_after_session = false
```

At runtime the daemon verifies that the configured Moonlight binary exists and reports version `6.0.0` or newer, prepares `state_dir` plus `portable.dat`, and launches `moonlight stream <host-authority> <app> --display-mode fullscreen` from that managed workspace. If `base_port` differs from `47989`, the relay renders `host:port` for hostnames and IPv4 or `[ipv6]:port` for IPv6 literals before invoking Moonlight.

Sunshine setup and any required pairing state in the managed Moonlight workspace remain operator-managed in this slice. Specs 31 and 32 are reserved for pair-assist and richer Moonlight launch/recovery behavior.

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

When VNC or Looking Glass is selected, the same state file also includes backend-specific troubleshooting metadata:

- `vnc_endpoint`
- `looking_glass_shm_file`

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
- `relayinner-display.console` for `remote-viewer`, `looking-glass-client`, or `moonlight` launch, exit, and preflight problems
- `relayinner-display.display` for display power helper failures
- `relayinner-display.input` for host power-button validation or evdev read failures
- `relayinner-display.session` for session connection and state-transition events

If the kiosk journal shows the `libseat` sequence `Could not connect to socket /run/seatd.sock: Permission denied`, `Could not open target tty: Permission denied`, `Timeout waiting session to become active`, or `Unable to create the wlroots backend`, confirm the installed kiosk unit launches `cage -- /usr/local/lib/relayinner-display/session-entrypoint` and that `relayinner-display-seatd.service` is up with the relay group owning `/run/seatd.sock`. Older installs that still launch `seatd-launch -- cage -- ...` can fail immediately with `status=1`; rerun `sudo ./install.sh` to refresh the units before debugging further. If `relayinner-display-seatd.service` instead fails with `Failed at step EXEC spawning /usr/bin/seatd`, rerun `sudo ./install.sh` so the installer refreshes the seatd unit with the actual host binary path, such as `/usr/sbin/seatd`, then restart `relayinner-display-seatd.service`, `relayinner-displayd.service`, and `relayinner-display-kiosk.service`.

If the kiosk journal shows `failed to open /dev/dri/renderD128: Permission denied`, `failed to open /dev/dri/card0: Permission denied`, or `Unable to create the wlroots renderer`, the kiosk service lacks the GPU groups required to open the DRM nodes. The installer now detects the owning groups for `/dev/dri/card*` and `/dev/dri/renderD*` and renders them into `SupplementaryGroups=` for `relayinner-display-kiosk.service`; rerun `sudo ./install.sh` and verify the unit contains the expected groups, typically `video render`.

The managed kiosk unit also exports `LIBSEAT_BACKEND=seatd` so `cage` uses the same backend as the successful transient `systemd-run` checks instead of depending on libseat backend auto-selection.

If `systemctl status relayinner-display-kiosk.service` briefly shows the child process as `/usr/bin/python3 /usr/local/lib/relayinner-display/session-entrypoint` before `cage` exits with `status=1`, the installed runtime is likely older than the absolute-path launcher hotfix. Refresh `/usr/local/lib/relayinner-display/relayinner_display/kiosk.py` with `sudo ./install.sh`; older copies tried to exec `relayinner-display-session` by bare name, which fails when `cage` does not preserve the kiosk unit `PATH`.

If `runuser -u relayinner-display -- /usr/local/lib/relayinner-display/session-entrypoint` or the kiosk journal shows `PermissionError: [Errno 13] Permission denied: '/etc/relayinner-display/config.toml'`, the session user cannot read the preserved host config. Refresh the install with `sudo ./install.sh`; the current installer normalizes `/etc/relayinner-display/` to service-group-readable permissions so the unprivileged kiosk session can load the same config as the root daemon.

If the display helper logs `Wayland server does not support wlr-output-power-management-v1`, the host is still configured for `wlopm`. Cage supports output management through `wlr-randr` more broadly than it supports the `wlopm` protocol, so rerun `sudo ./install.sh` and keep `power_helper = "wlr-randr"` unless you have confirmed `wlopm` support on that compositor build.

If the daemon logs `Prepared console launch for VM ... with backend=spice` and `Console started: backend=spice pid=...` but the viewer immediately closes with a dialog like `Unable to connect graphic server /run/relayinner-display/console/spice-current.vv`, inspect the generated `.vv` file. Proxmox returns the `ca` certificate with escaped newlines, and the relay writer must keep that value escaped on one `ca=...` line; literal embedded newlines break the INI format and can cause `remote-viewer` to fail before the SPICE session opens.

If the state file or journal shows `console_backend=vnc` and the appliance moves into `degraded`, verify that `qm config <vmid>` contains a loopback-only `args: -vnc 127.0.0.1:<display_number>` entry and that `[console.vnc].display_number` matches it exactly. Non-loopback binds such as `0.0.0.0` are refused intentionally.

If the VNC backend stays in the reconnecting path while the VM is running, inspect `vnc_endpoint` in `/run/relayinner-display/daemon.state.json` and confirm the derived host-local port is actually listening before `remote-viewer` launches. The relay probes the loopback endpoint before each launch and waits in reconnect flow until the socket accepts TCP connections.

If the state file or journal shows `console_backend=looking-glass` and the appliance moves into `degraded`, verify that `looking_glass_shm_file` matches the expected KVMFR or IVSHMEM device, that the file already exists before the daemon starts, and that the `relayinner-display` session user can read it. Missing or unreadable shared memory is treated as a hard preflight failure by design.

If Looking Glass reconnects repeatedly while the guest stays up, inspect the console log lines for `backend=looking-glass` and confirm the guest-side host application plus upstream passthrough setup are healthy. The relay only validates host-visible prerequisites; it does not diagnose guest-side capture or passthrough failures beyond that boundary.

If the state file or journal shows `console_backend=moonlight` and the appliance moves into `degraded`, verify that Linux `moonlight-qt` `6.0.0` or newer is installed, that `[console.moonlight].host` and `base_port` point at the expected Sunshine host, and that the configured `state_dir` plus `portable.dat` are present and writable by the `relayinner-display` session user. The current implementation prepares the workspace and launches Moonlight from it, but any required pairing state inside that workspace is still operator-managed.
