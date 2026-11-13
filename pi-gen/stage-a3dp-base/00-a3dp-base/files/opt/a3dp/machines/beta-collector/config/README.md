# beta-collector profile config overlay

This profile is not a printer: no printer.cfg semantics are deployed (the
base placeholder printer.cfg remains and klipper is disabled — see
`disable.list`). The overlay exists to keep the firstboot profile merge
uniform and to ship the camera lane config:

- `crowsnest.conf` — bundled 1080p USB webcam on ustreamer :8080, MJPEG
  (the crowsnest installer default is 640x480; inadequate for the dataset).
  The collector snapshots `http://127.0.0.1:8080/?action=snapshot`.
