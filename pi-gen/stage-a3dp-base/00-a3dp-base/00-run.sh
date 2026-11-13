#!/bin/bash -e
# Host-side file placement (runs before the -run-chroot scripts).
# pi-gen has no built-in files/ mechanism — each stage copies its own tree
# into $ROOTFS_DIR. Ownership stays root here; chroot scripts fix up what
# needs to be pi-owned (e.g. printer_data, moonraker.conf).
cp -a files/. "${ROOTFS_DIR}/"
