{
  description = "Boot-time in-RAM VBIOS power-policy edits and runtime current-limit control for one NVIDIA laptop GPU (Acer PT516-52s)";

  # No inputs on purpose. The module builds against the consumer's own nixpkgs
  # (config.boot.kernelPackages and pkgs), so nothing here should pin one. The
  # CPU checks have their own pin in shell.nix.
  outputs =
    { self }:
    {
      nixosModules.default = ./module;
      nixosModules.pt516GpuPower = self.nixosModules.default;
    };
}
