# Tape Chart for Home Assistant

Connects Home Assistant to **Tape Chart**, the hotel PMS: every room of a property becomes a
device in Home Assistant that knows who is staying in it, and the room's sensors (presence,
door, thermostat, …) show up live in the PMS.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Hotel-Assistant&repository=tapechart-ha&category=integration)

## What you get

One device per room, named `<property> · <room number>`, with:

| Entity | Value |
|---|---|
| Status | `vacant`, `arriving`, `occupied`, `departing`, `departed`, `out_of_service` (attributes: business date and, when the PMS sends names, the guest and companions) |
| Guest checked in | on while the stay is checked in |
| Guest language | `bg`, `en`, … |
| Adults / Children | counts; Children has the attribute `ages` |
| Check-in / Check-out | timestamps with the stay's times (property time zone) |
| Nights / Nights left | counts |
| Next arrival | timestamp of the next booking |
| Stay (event) | `checked_in`, `checked_out`, `moved_in`, `moved_out`, `stay_changed`, `cancelled` |

Use them in automations: heat the room before arrival, run a welcome scene on check-in, switch to
eco mode on check-out.

In the other direction you choose, per room, which Home Assistant entity plays which role:
presence, key card, door, window, do not disturb, make up room, thermostat, room temperature,
controller online, alarm, and three **scenarios** (welcome, eco, all off) that front-desk staff may
run from the PMS. **Only the entities you map ever leave Home Assistant.**

One Home Assistant can serve several properties: pair each one (one integration entry per
property).

## Requirements

- Home Assistant 2025.6 or newer.
- The PMS must be able to reach Home Assistant: a LAN address when the PMS runs in the hotel
  network, otherwise an external URL or Home Assistant Cloud (Nabu Casa). Set them under
  **Settings → System → Network**.

## Install

### HACS (recommended)

1. Click the badge above, or in HACS open **⋮ → Custom repositories**, add
   `https://github.com/Hotel-Assistant/tapechart-ha` with type **Integration**.
2. Install **Tape Chart** and restart Home Assistant.

### Manual

1. Download `tapechart.zip` from the latest release (or copy the folder
   `custom_components/tapechart` from this repository).
2. Put its contents in `/config/custom_components/tapechart/` so that
   `/config/custom_components/tapechart/manifest.json` exists.
3. Restart Home Assistant.

## Pair with the PMS

1. In Tape Chart open **Settings → Integrations → Home Assistant**, choose the property and press
   **Generate pairing code**. The page shows the PMS address and a code like `ABCD-EFGH`, valid for
   10 minutes.
2. In Home Assistant open **Settings → Devices & services → Add integration → Tape Chart**.
3. Type the PMS address and the code. Home Assistant tells the PMS where it can be reached; the PMS
   checks that it can call back, and the pairing is done.
4. The rooms appear as devices. Pairing the same property again (for example after moving the PMS)
   updates the existing entry.

The PMS address must use `https://`, unless it is in the local network (a private IP address or a
`.local` / `.lan` name).

## Map the rooms

Open **Settings → Devices & services → Tape Chart → Configure**:

- **Map a room** — choose the room, optionally the device that serves it (for example its ESPHome
  controller; it is suggested from the room number in the device name), then one entity per role.
  If the device has no connectivity sensor, tick **Online = the device is available**: the room is
  reported offline when every entity of the device is unavailable.
- **Fill other rooms by pattern** — map one room, then let the integration find the same entities
  for the other rooms: first inside each room's own device (matched by the entity's unique id or
  name, so it works even when entity ids are named differently), then by putting the room number
  into the entity id (`binary_sensor.room_205_bathroom_pir` → `binary_sensor.room_206_bathroom_pir`).
  You see what was found and what was not before anything is saved.
- **Settings** — how long presence must be off before "off" is sent (default 5 minutes), and the
  smallest temperature change (0.5°) and shortest interval (60 s) for temperature updates.

## Privacy

- Guest names are sent by the PMS only while **Send guest names** is on in Tape Chart. They are
  never an entity state: they are attributes of the room's Status sensor, excluded from the
  recorder (history), and removed when the stay ends. Booking numbers, contact data and money are
  never sent.
- Nothing about your Home Assistant leaves it except the entities you map (id, name, state), and
  at pairing its instance id, version, location name and the URLs the PMS uses to reach it.
- The shared secret of the pairing is stored in the integration's config entry, so it is part of
  your Home Assistant **backups**. Treat backups accordingly; if one leaks, generate a new pairing
  code and pair again — the old secret stops working at once.
- Diagnostics downloads leave out the secret, the webhook id and guest names.

## Troubleshooting

- **"Tape Chart could not reach this Home Assistant"** — the PMS tried every URL Home Assistant
  offered. Check the internal/external URL under **Settings → System → Network**, a firewall
  between the PMS and Home Assistant, or enable Home Assistant Cloud.
- **"The pairing code has expired / is not valid"** — generate a new code; each code works once.
- **A repair "Tape Chart: … is disconnected"** — the property was disconnected in the PMS or paired
  with another Home Assistant. Generate a new code and use the reauthentication prompt of the
  integration. Your room mapping is kept.
- **Signals do not show up in the PMS** — check that the room is mapped (Configure → Map a room)
  and download the diagnostics: `outbox_size` and `outbox_last_error` show whether events are
  waiting and why. Events are kept on disk and sent when the PMS is reachable again.
- Debug logging:

  ```yaml
  logger:
    logs:
      custom_components.tapechart: debug
  ```

## Development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/pytest -q
```

`scripts/i18n.py` writes `translations/en.json` and `translations/bg.json`; `scripts/brand.py`
draws the brand images. Releases are tagged `vX.Y.Z` matching `manifest.json`; publishing a
GitHub release attaches `tapechart.zip`, which HACS installs.

## License

MIT
