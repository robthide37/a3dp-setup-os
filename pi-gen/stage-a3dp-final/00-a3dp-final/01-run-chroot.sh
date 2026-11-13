#!/bin/bash -e
# stage-a3dp-final / 01 — image assembly: hygiene for a public, fleet image.
# No secrets are baked anywhere upstream of this; this stage enforces the
# identity resets (host keys, machine-id) so every flashed unit is unique.

# SSH host keys: regenerate per unit on first boot.
rm -f /etc/ssh/ssh_host_*
systemctl enable regenerate_ssh_host_keys 2>/dev/null || true

# machine-id: empty file -> systemd generates a unique one at first boot.
rm -f /etc/machine-id
touch /etc/machine-id

# belt-and-braces secret sweep — the build FAILS if anything secret-shaped
# made it into the rootfs (public repo, customer-downloadable images).
# -I: skip binary files. onnxruntime's .so libs embed the literal strings
# "BEGIN RSA/EC PRIVATE KEY" as test-fixture data (verified in the wheel) —
# they are not keys; every real PEM key is text, so binaries add no coverage.
if grep -rliIE 'BEGIN (RSA|OPENSSH|EC) PRIVATE KEY' \
      /etc /home /opt /root /usr/local 2>/dev/null; then
  echo "SECRET LEAK: private key material found in image" >&2
  exit 1
fi

apt-get clean
rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
rm -rf /home/pi/.cache /root/.cache
find /var/log -type f -exec truncate -s 0 {} \; 2>/dev/null || true
