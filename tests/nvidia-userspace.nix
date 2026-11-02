# Exercise the packaged ELF loader without opening NVIDIA devices.
# Both the enabled module and the disabled/default-driver route must start.
{ pkgs, kernelPackages }:
pkgs.runCommand "gpu-power-nvidia-userspace-tests" { } ''
  mkdir "$out"
  ${kernelPackages.nvidiaPackages.latest.bin}/bin/nvidia-smi --help > "$out/latest"
  ${kernelPackages.nvidiaPackages.stable.bin}/bin/nvidia-smi --help > "$out/stable"
  grep -q 'NVIDIA System Management' "$out/latest"
  grep -q 'NVIDIA System Management' "$out/stable"
''
