#!/bin/bash
# a3dp-setup-os build orchestrator.
# Fetches pi-gen at a pinned commit, copies our stages + config in, builds the
# image with pi-gen's docker wrapper. Output: pigen-upstream/deploy/*.img.xz
#
# Works natively on arm64 (fast) or under qemu on x86_64 (slow but functional —
# pi-gen's docker image includes qemu-user-static).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIGEN_DIR="${HERE}/pigen-upstream"
PIGEN_REPO="https://github.com/RPi-Distro/pi-gen.git"
PIGEN_PIN="6e3ff62bfd9c690a6a3d1d7de259d5e0af40cb78"   # pinned for reproducible builds

# ---- fetch pi-gen at the pin ----
if [ ! -d "$PIGEN_DIR/.git" ]; then
  git clone "$PIGEN_REPO" "$PIGEN_DIR"
fi
git -C "$PIGEN_DIR" checkout --detach "$PIGEN_PIN"

# ---- stage our stages + config ----
rm -rf "$PIGEN_DIR"/stage-a3dp-base "$PIGEN_DIR"/stage-a3dp-final
cp -r "$HERE/pi-gen/stage-a3dp-base" "$PIGEN_DIR/"
cp -r "$HERE/pi-gen/stage-a3dp-final" "$PIGEN_DIR/"
cp "$HERE/pi-gen/config.a3dp" "$PIGEN_DIR/config"

# only the final stage produces an image
for s in stage0 stage1 stage2 stage-a3dp-base; do
  touch "$PIGEN_DIR/$s/SKIP_IMAGES"
done

# ---- build ----
cd "$PIGEN_DIR"
HOST_ARCH="$(uname -m)"
if [ "$HOST_ARCH" = "arm64" ] || [ "$HOST_ARCH" = "aarch64" ]; then
  # Native arm64 build: pi-gen's docker wrapper uses an i386/debian base
  # (x86-host/qemu layout) which cannot exec on an arm64 runner. Native is
  # the supported arm64 path and needs root (sudo on CI).
  echo "arm64 host ($HOST_ARCH): native pi-gen build"
  if [ -x /usr/bin/apt-get ] && command -v sudo >/dev/null 2>&1; then
    # pi-gen depends-list (repo pin): quilt parted coreutils qemu-user-binfmt
    # debootstrap zerofree zip dosfstools e2fsprogs libcap2-bin grep rsync
    # xz-utils curl xxd file git kmod bc gpg pigz arch-test
    sudo apt-get -y update
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y install --no-install-recommends \
      quilt parted coreutils qemu-user-binfmt debootstrap zerofree zip \
      dosfstools e2fsprogs libcap2-bin rsync xz-utils curl xxd file git \
      kmod bc gpg pigz arch-test
  fi
  sudo -E ./build.sh -c config
elif command -v docker >/dev/null 2>&1; then
  CONTINUE=1 ./build-docker.sh -c config
else
  # native fallback (x86_64 hosts without docker): slow under qemu, needs root
  sudo -E ./build.sh -c config
fi

echo "Image: $PIGEN_DIR/deploy/"
ls -lh "$PIGEN_DIR/deploy/" || true
