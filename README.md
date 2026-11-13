# a3dp-setup-os — A3DP fleet OS image (pi-gen)

pi-gen pipeline that builds **one base image for the whole A3DP printer fleet**
(Rift 525 machines + the IDEX big-meter kits) on Raspberry Pi OS Lite 64-bit.
Per-machine activation happens at **first boot** from a machine profile — there is
one `.img`, not one image per machine.

Base image contents (everything is baked in, nothing is flashed by hand):

| Layer | What's in it |
|---|---|
| PiOS Lite 64-bit (trixie) | boots Pi 4 **and** Pi 5 |
| Kalico | pinned to the fleet firmware protocol commit (`84b3e373` — the version all fleet MCUs already run) |
| Moonraker | canonical `moonraker.conf` (printer_data layout, UDS socket, update-manager entries, RFC1918 trusted clients) |
| Mainsail + nginx | A3DP `.theme` skin pre-deployed |
| Crowsnest + ustreamer | camera stack (validated live on a3dpmeter; service may report "no devices" until a camera is plugged in — see Known behavior) |
| katapult + pyserial | bootloader repo + flashtool (meter kit toolboards boot through it) |
| `a3dp-mcu-flashd` | Klipper-independent recovery flasher, :8082 (nginx LAN allowlist) |
| Tailscale | client + `tailscaled` — installed **logged out**, zero auth material; remote access per [Customer onboarding](#customer-onboarding) |
| moonraker-mcu-flasher | UI flash/update path (`FLASH_MCU` macro) |
| klipper host-mcu | `klipper_mcu` + `klipper-mcu.service` (Rift `[mcu rpi]`) |
| Cross toolchain | `gcc-arm-none-eabi` — on-box firmware builds for the UI flasher |
| AI stack (staged) | `a3dp-bedgate` daemon + print monitor — installed, **disabled**; enabled per machine profile |
| Templates | DHT drybox MQTT template at `/opt/a3dp/templates/dht-mqtt/` (never baked/installed) |
| Config backup | daily `a3dp-config-backup.timer` (printer_data/config → 14 rotating snapshots) |
| Host performance | klippy pinned to a dedicated core (`CPUAffinity=3`) + `Nice=-10`; crowsnest/ustreamer + AI timers nice'd down; **no swap** (dphys-swapfile disabled — SD swap ruins motion timing) |

Machine profiles shipped in the image (first boot selects ONE):

| Profile | Matches hostname prefix | Deployed config |
|---|---|---|
| `rift-525` | `rift*`, `a3dp525*` | live-verified a3dp525 config (stm32f446 main + stm32g0b1 head0, by-id product-string paths), bed-gate macros + daemon (enabled), Cartographer/eddy probe config, timelapse |
| `meter-idex` | `meter*`, `a3dpmeter*` | meter kit config (Octopus Pro H723 + 2× LDO NiteHawk-36), Dual.cfg IDEX, euclid.cfg, mcu-flasher sections; MCU by-id paths tokenized — filled from live enumeration at first boot |
| `base` | anything else | placeholder config; all AI services off |

## Flash + activate

1. **Flash with Raspberry Pi Imager** → choose OS → "Use custom" → the released
   `.img.xz` (Imager writes compressed images directly).
2. **Imager OS customization** (this is the per-machine activation):
   - **Hostname** — the profile selector (see table above). This is how you pick
     the machine type. Everything else is automatic at first boot.
   - **SSH** — enable + authorize your key (no keys/credentials ship in the image).
     Don't want to use Imager's SSH page? Drop your public key(s) on the boot
     partition as `a3dp-ssh.pub` instead — first boot installs them and enables
     sshd. With neither, the machine ships with **SSH off and no credentials**;
     Mainsail on the LAN still works (see Customer onboarding).
   - Optional: set the `pi` user password here if you want password login. The
     image ships the `pi` account **locked** with passwordless sudo
     (`/etc/sudoers.d/010_pi-nopasswd`) — SSH keys are the intended access.
   - Optional: **client UI** — for a customer-facing unit, add a file
     `a3dp-machine.conf` on the boot partition containing
     `A3DP_CLIENT_UI=1` (or an empty file named `a3dp-client-ui`). First boot
     curates Mainsail to the phone-friendly client preset (status/progress,
     temps, webcam, start/pause/stop, e-stop — operator pages hidden) and
     seeds a **Service** macro group (fixed UUID
     `a3d0c1de-0000-4000-8000-00000000a3d0`, moonraker db `mainsail.macros`)
     holding `REMOTE_SETUP` + `RESTART_MONITOR` — the only macro group the
     preset keeps visible, so onboarding needs no SSH. Toggle later without a
     re-flash: `sudo a3dp-client-ui on|off` (re-seed if moonraker was down
     during first boot: `sudo a3dp-client-ui seed`).
   - Locale/timezone as usual.
3. Boot. First boot runs `a3dp-firstboot` (log: `/var/log/a3dp-firstboot.log`)
   which deploys the profile config, enumerates MCU by-id paths, enables the
   profile's services, and restarts klipper/moonraker.
4. **Fleet ops (post-flash, local)**: install the fleet SSH key, drop any
   unit-specific extras (DHT drybox template is at
   `/opt/a3dp/templates/dht-mqtt/README.md`), point crowsnest at the unit's
   camera (`/dev/v4l/by-id/`).

**Raw-dd flashes (no Imager customization)**: etch the released `.img.xz` with
dd/Etcher and the card still activates — mount the card's boot (FAT) partition
and drop two files before first boot:
- `a3dp-machine.conf` containing `A3DP_MACHINE=<profile>` (`rift-525`,
  `meter-idex`, or `base`) — the profile selector when Imager isn't there to
  set the hostname.
- `a3dp-ssh.pub` with your public key(s), one per line — first boot installs
  them and enables sshd (LF or CRLF line endings both work; keys are deduped,
  comments/blank lines ignored).

### Flash-time credentials (per-unit only — never in the image)

The public image ships with **zero credentials**: `pi` locked, SSH off. Two
per-unit exceptions, both applied at flash/bake time for a *specific* unit,
never in the released image:

- **Customer-standard password**: a customer whose fleet standardizes on a
  password (e.g. `pi=<their password>`) gets it set in the pre-bake/flash
  step for their cards (Imager customization or a post-flash `passwd`).
- **A3DP fleet cards**: the fleet SSH key is injected post-flash (existing
  fleet convention).

Anything baked this way lives only on that unit's card.

### Support access (baked in, opt-out for customers)

The image ships a dedicated **`a3dp-support`** user (no sudo, shell only) whose
`authorized_keys` contains A3DP's support public key
(`a3dp-image-support-20261113`). It is inert unless SSH is enabled (flash-time
choice); day-to-day support rides Tailscale SSH. Customers can revoke it:

```
sudo deluser a3dp-support
```

The private half of the key never ships — it lives in A3DP's secrets store
only, never in the fleet key or any image artifact.

### Overriding the profile without Imager

Drop a file `a3dp-machine.conf` on the boot partition containing
`A3DP_MACHINE=rift-525` (or `meter-idex`, `base`) — it wins over the hostname map.

## Customer onboarding

The full path from a fresh flash to a supported, optionally AI-monitored machine.
Nothing here needs A3DP to touch the customer's network.

### 1. LAN access (works out of the box)

After the first boot completes (~2 min; watch the status LED or just retry),
Mainsail is reachable on the customer's LAN at `http://<hostname>.local/`
(mDNS) or `http://<printer-ip>/` — no SSH, no login (Moonraker trusts RFC1918
LAN + Tailscale CGNAT sources only; it is not exposed to the internet).

### 2. Remote access — customer's own tailnet (recommended)

Tailscale is pre-installed but **logged out** — the image carries zero auth
material. The customer joins *their* tailnet without SSH, straight from the
printer UI: **Dashboard → Service → `REMOTE_SETUP`** (Mainsail macro button;
the Service group is seeded by the image, see below) or the same macro on
KlipperScreen. The macro starts the login in the background and prints the
auth URL to the console **and** writes it to `tailscale-auth-url.txt` in the
Jobs list — the customer opens that URL on a device signed into their
Tailscale account and approves the printer (~1 min). The printer is then
reachable from all their devices at its Tailscale name, e.g.
`http://a3dp-rift1:80/` — Mainsail and the camera work unchanged.

(SSH alternative: `sudo tailscale up` on the unit does the same.)

### 3. Support access — share the node to the A3DP tailnet

For A3DP support (and AI monitoring, step 4), the customer shares the printer
node with A3DP from their own Tailscale admin console
(**Machines → ⋯ → Share → invite `robthide37@gmail.com`**); Rob accepts the
invite. A3DP then reaches the machine over the tailnet without ever touching
the customer's router.

**Verified constraint (2026-11-05, see KB `ts-node-sharing-facts`):** a shared
node accepts *inbound* connections but the share is one-directional — the
customer's printer can NOT open connections to A3DP nodes, and A3DP's
*tagged* nodes (tag:pve, tag:ai-vm) cannot initiate connections to it either.
Support sessions therefore originate from a **user-owned** A3DP node (laptop /
.231 / the PVE printer gateway once it exists — t_9b92f656). SSH, Mainsail and
Moonraker are all reached this way (live-proven on Brent's shared-in meter).

### 4. AI opt-in (bed-occupancy gate + print monitoring)

The AI stack ships **installed but disabled**; A3DP enables it per machine,
only when the customer opts in. Two pieces:

- **Bed gate (`a3dp-bedgate`)** — rift-525 profile enables the daemon at first
  boot, but it is fail-open and dormant until a detection lane feeds it
  verdicts (KB `GATE-CONTRACT.md`). Safe either way; no credentials involved.
- **Print monitor** — needs the llama-swap API key + confirm token. Both are
  **never baked**; they land as Mainsail-editable files:
  1. Customer (or A3DP support) opens the Mainsail file manager → `a3dp/`
     folder and creates/pastes two values: `llamaswap.key` and
     `confirm.token` (the services read them through symlinks from
     `/etc/a3dp-print-monitor/` — perms preserved, nothing committed).
  2. Run the **`RESTART_MONITOR`** macro (Dashboard → Service in Mainsail /
     KlipperScreen) — it restarts the enabled monitor services. Done, no SSH.
  The VLM endpoint in `/home/pi/monitor/config.ini` must be the
  **tailnet-reachable** llama-swap address (see the topology note below) —
  never a LAN IP, which is unroutable from a customer unit.

**Topology constraint (verified):** because of the one-way share above, the
customer's Pi cannot call A3DP's llama-swap VLM lane directly. Detection
either (a) runs through an A3DP **relay node shared back into the customer's
tailnet** — the Pi then calls that relay's Tailscale address (same pattern
already live for Brandon's devices → ai-services grants), or (b) moves the
VLM call to the A3DP side (pull the snapshot + push verdicts back via
Moonraker — both are inbound to the printer, which the share permits). Option
(b) needs monitor changes; until one of the two lands (pinned to the
printer-gateway work, t_9b92f656), AI detection on customer units is
**documented, not yet enabled**. Enabling the monitor services on an opted-in
unit is a support step over the tailnet SSH path:
`sudo systemctl enable a3dp-print-monitor.timer a3dp-confirm-listener.service`.

### 5. Support path summary

| Need | How |
|---|---|
| Look at the printer | Mainsail via tailnet from a user-owned A3DP node |
| Fix config / diagnose | SSH via tailnet (customer key or our fleet key) |
| Flash an MCU | on-box web flasher :8082 — LAN-only by design; do it over SSH tunnels or on-site |
| Bed-gate UI | :8083 — LAN-only by design (dangerous operations stay off the tailnet) |
| Push files | `scp` over the tailnet |

## Build

```bash
./build.sh            # docker + pi-gen; output in pigen-upstream/deploy/*.img.xz
```

GitHub Actions builds on every `v*` tag (arm64 runner, native) and attaches
`<image>.img.xz` + `SHA256SUMS.txt` to the release. `workflow_dispatch` builds
without publishing.

## Security posture (public repo, customer-downloadable images)

- **No secrets baked in**: no SSH keys, tokens, WiFi credentials, API keys, or
  passwords in the image or this repo. Scan before any push:
  `grep -rniE '(token|password|secret|api[_-]?key)[ ]*[=:][ ]*['"'"'"]?[A-Za-z0-9+/]{16,}' .`
- `pi` account ships **locked**; access = SSH keys you inject via Imager.
- SSH host keys are regenerated on first boot (`regenerate_ssh_host_keys`);
  `/etc/machine-id` is reset so every flashed unit gets a unique identity.
- Moonraker `trusted_clients` + the nginx allowlists cover RFC1918 private
  ranges plus the Tailscale CGNAT range (100.64.0.0/10 — needed for the
  tailnet support path); the appliance is otherwise LAN-only. The MCU flasher
  (:8082) and bed-gate (:8083) nginx allowlists stay RFC1918-only on purpose:
  dangerous operations are not exposed to tailnet sources.
- **Tailscale ships logged out** — the package is pre-installed and
  `tailscaled` idles, but no auth key, state, or node identity is baked. The
  customer (or A3DP support) authenticates it per the Customer onboarding
  section; until then the node joins no tailnet.
- The print monitor needs a llama-swap API key and a confirm token — **never
  baked**; they are post-install files (`/etc/a3dp-print-monitor/*`, 0600) and
  the monitor services ship disabled.

## Repo layout

```
build.sh                     fetch pinned pi-gen, copy stages, run docker build
pi-gen/config.a3dp           pi-gen config (trixie, arm64, user pi)
pi-gen/stage-a3dp-base/      everything above (00-packages, run.sh stages, files/ tree)
pi-gen/stage-a3dp-final/     image assembly stage (cleanup, machine-id/host-key reset)
.github/workflows/build-image.yml
```

The image stages fold in the logic of the live-validated
`scripts/a3dp-host-install.sh` (printer-hardware repo; first live run
2026-10-31, 9 deviations found + fixed — all folded in here, notably:
unattended Crowsnest install, manual crowsnest moonraker entry, rewritten
klipper.service with `-a` UDS socket, timesync via systemd-timesyncd only).

## Known behavior (expected, not bugs)

- **Crowsnest** reports "No usable Devices Found" until a camera is physically
  connected, and the failed unit will NOT retry on its own — run
  `sudo systemctl reset-failed crowsnest` (or reboot) after plugging the cam.
- **Crowsnest installer** exits 1 unattended even on success; the moonraker
  update entry is added by us instead (idempotent, in the canonical conf).
- **meter-idex with no MCUs connected**: klippy stays down ("unable to open
  serial") until boards are connected — by-id paths are filled from live
  enumeration at first boot, never guessed.
- **rift-525 config** ships the unit-tuned SAVE_CONFIG (bed mesh, bed PID) of the
  reference a3dp525 as family defaults; re-tune per unit at bench.
