#!/usr/bin/env bash
# shellcheck disable=SC2034
# Immutable upstream inputs. Driver source commit is the dereferenced 615.78.08 tag.
version=615.78.08
# Read-only stock captures adjudicated in the four-OS Phase 2 VM round.
fixture_version=615.78.08
source_commit=1a82fbfda8f321b056eb3568850f9316c5e465c5
editor_commit=edf0f483d8d4a3d7a578f941647c95b503ab9795
driver_sha256=3e3f6dddc2ee767a08185300af7be3cb286ece76698d2dde3524599782d07ffe
# archive.archlinux.org packages fetched and hashed 2026-11-01; pacman verifies .sig too.
nvidia_utils_sha256=73cf243eae4b73a9681760305420b03e19ac699f120effd2f57888b5f4725a84
lib32_nvidia_utils_sha256=19bce81e0c29a17b0978ec6797f15b7f21fbf8e42c9b026728da1e1e59d54cca
modules='nvidia nvidia-modeset nvidia-drm nvidia-uvm nvidia-peermem'
nvidia_utils_pkgrel=2
lib32_nvidia_utils_pkgrel=1
