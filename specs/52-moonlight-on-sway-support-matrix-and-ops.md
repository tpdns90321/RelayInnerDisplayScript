# Spec 52. Moonlight on Sway Support Matrix and Ops

## Context / Problem

Specs 30 through 32 and Spec 40 define the current Moonlight backend contract, pairing, stream launch, reconnect behavior, and `Desktop` fast-path semantics. They still inherit an older kiosk assumption:

- the product narrative treats `cage` as the universal kiosk shell
- the install and troubleshooting docs point operators at one compositor path
- the runtime does not yet formalize which compositor and backend combinations are actually supported

Once Specs 50 and 51 add compositor selection and a managed sway runtime, the project needs an explicit support matrix and operator contract. Without that clarification:

- Moonlight operators may keep trying unsupported Cage paths
- docs will keep mixing current Cage guidance with the new supported sway path
- troubleshooting will remain ambiguous about whether a failure is in pairing, app validation, stream launch, or compositor/output setup

## Goals / Non-goals

Goals:

- Define the supported compositor and backend matrix after Specs 50 and 51.
- Make Moonlight-on-sway the documented supported path.
- Keep Cage as the documented supported path for SPICE, VNC, and Looking Glass.
- Update install, config, and troubleshooting docs so compositor selection is explicit.
- Keep Moonlight runtime diagnostics readable by distinguishing compositor/output issues from later Moonlight stages.

Non-goals:

- Reworking the Moonlight pairing or stream-launch protocol itself.
- Adding support for Moonlight on Cage.
- Adding support for non-Moonlight backends on sway.
- Introducing a graphical compositor selector UI.
- Expanding the support matrix to more compositors beyond `cage` and `sway`.

## User stories

- As an operator using Moonlight, I want the docs to tell me directly that the supported kiosk path is sway rather than Cage.
- As an operator using SPICE, VNC, or Looking Glass, I want the docs to confirm that my path still uses Cage by default.
- As a troubleshooter, I want logs and runtime state to tell me whether a failure is before Moonlight stream startup or inside the Moonlight flow itself.
- As a maintainer, I want the support matrix documented in one place instead of inferred from config parsing or launcher code.

## Public API / Interfaces

Support matrix after this series:

| console backend | supported compositor |
| --- | --- |
| `spice` | `cage` |
| `vnc` | `cage` |
| `looking-glass` | `cage` |
| `moonlight` | `sway` |

Configuration examples:

```toml
[target]
console_backend = "moonlight"

[kiosk]
compositor = "auto"
```

Documented default behavior:

- `moonlight + auto` resolves to `sway`
- `spice|vnc|looking-glass + auto` resolve to `cage`

Runtime diagnostics contract additions:

- startup logs for Moonlight must include both `backend=moonlight` and `kiosk_compositor=sway`
- troubleshooting docs must classify failures into:
  - compositor or output setup
  - pairing
  - app validation
  - stream launch or reconnect

Documentation updates required by this spec:

- root `README.md`
- `specs/README.md`
- sample config comments
- `docs/proxmox-host-setup.md`

Documentation rules:

- Moonlight docs must describe the managed sway path as the supported compositor path.
- Cage troubleshooting docs must stay in place for the non-Moonlight backends.
- Moonlight troubleshooting docs must stop implying that Cage is the expected compositor for that backend.

## Data model / Persistence

Persistent config remains unchanged from Specs 50 and 51:

- `/etc/relayinner-display/config.toml`

Runtime diagnostics remain file-backed:

- `/run/relayinner-display/daemon.state.json`

Persistence rules:

- This spec adds no new persistent config keys beyond the `[kiosk]` table already introduced by Spec 50.
- This spec adds no new persistent Moonlight state.
- This spec relies on the existing `kiosk_compositor`, `moonlight_pair_state`, and `moonlight_app` runtime fields.

## Security model (Permission/Isolation/Audit)

Permissions:

- Moonlight continues to run as the relay session user inside the supported sway kiosk path.
- This spec does not add any new secret material, credentials, or privilege transitions.

Isolation:

- The support matrix remains curated and explicit; unsupported compositor combinations are not documented as experimental operator options.
- The relay continues to treat Moonlight as a curated backend, not a general desktop app launcher.

Audit:

- Ops docs must tell operators which service logs matter for compositor startup versus Moonlight runtime.
- Runtime logs must preserve backend-tagged and subsystem-scoped failure messages so operators can distinguish setup from stream-stage failures.

## Acceptance criteria (Testable, Verifiable)

- README, setup docs, and sample config all describe Moonlight as a supported sway-based path.
- README, setup docs, and sample config all keep SPICE, VNC, and Looking Glass on the Cage path.
- Runtime troubleshooting text distinguishes compositor/output failure from Moonlight pairing, app validation, and stream launch failure.
- `kiosk_compositor` appears in runtime diagnostics and is referenced by the documentation.
- Existing Moonlight pairing, `Desktop` fast-path, non-`Desktop` app validation, and reconnect semantics remain unchanged apart from the documented compositor path.

## Test plan

Documentation checks:

- root README links Specs 50 through 52
- spec index includes Specs 50 through 52
- setup guide describes Moonlight on sway and non-Moonlight on Cage
- sample config comments describe the compositor default correctly

Runtime checks:

- Moonlight startup log includes resolved compositor
- runtime state includes `kiosk_compositor = "sway"` on Moonlight
- runtime state includes `kiosk_compositor = "cage"` on SPICE, VNC, and Looking Glass

Manual operator checks:

- Moonlight path with default auto compositor and pairing flow
- Moonlight path with paired `Desktop` fast-path
- forced Moonlight exit while sway kiosk remains active
- non-Moonlight backend boot path still reaches Cage kiosk session

## Rollout / Backward compatibility

This spec is operational and documentary:

- it does not change the Moonlight wire protocol or session IPC
- it narrows the supported operator path by documenting Moonlight on sway explicitly
- it leaves non-Moonlight backends on their existing Cage path

Compatibility rules:

- older docs that describe Cage as the universal kiosk compositor must be updated
- support guidance must prefer the documented compositor matrix over ad hoc operator workarounds

## Open questions

- Whether a later spec should add compositor-specific troubleshooting hints directly into degraded reasons.
- Whether a future series should revisit non-Moonlight sway support after enough field evidence accumulates.
- Whether the install flow should eventually validate the support matrix proactively and print compositor-specific package hints before restart.

## Spec Dependencies

- Spec 50. Kiosk Compositor Selection Contract
- Spec 51. Managed Sway Kiosk Runtime
- Spec 30. Moonlight Backend Contract and Config
- Spec 31. Moonlight Pair-Assist and Persistent Workspace
- Spec 32. Moonlight Stream Launch, Recovery, and Ops
- Spec 40. Moonlight Desktop Fast-Path and Pair-State Resilience
