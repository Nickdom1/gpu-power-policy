{
  nixpkgs,
  module,
  pkgs,
}:
let
  inherit (nixpkgs) lib;
  makeSystem =
    {
      dynamicBoost ? false,
      systemdInitrd ? false,
      open ? true,
    }:
    lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        module
        ({ pkgs, ... }: {
          system.stateVersion = "24.11";
          nixpkgs.config.allowUnfree = true;
          boot = {
            kernelPackages = pkgs.linuxPackages_latest;
            loader.grub.enable = false;
            initrd.systemd.enable = systemdInitrd;
          };
          fileSystems."/" = {
            device = "/dev/vda1";
            fsType = "ext4";
          };
          services.xserver.videoDrivers = [ "nvidia" ];
          hardware = {
            nvidia.open = lib.mkIf (open != null) open;
            nvidia.dynamicBoost.enable = dynamicBoost;
            gpuPowerPolicy = {
              enable = true;
              device = "0000:01:00.0";
              # Evaluation must not attempt to build or read a policy image.
              vbios = builtins.toFile "evaluation-only.rom" "not a ROM";
              powerCeilingWatts = 140;
              powerDefaultWatts = 80;
              currentCeilingMilliamps = 120000;
              currentRatedMilliamps = 99000;
              powerdTargetWatts = if dynamicBoost then 125 else null;
            };
          };
        })
      ];
    };
  evaluate =
    settings:
    let
      c = (makeSystem settings).config;
      modprobe = c.environment.etc."modprobe.d/gpu-power-policy.conf".source;
    in
    assert c.systemd.services ? nvidia-powerd == settings.dynamicBoost;
    assert c.boot.initrd.extraFiles."etc/modprobe.d/gpu-power-policy.conf".source == modprobe;
    assert c.boot.initrd.systemd.contents."/etc/modprobe.d/gpu-power-policy.conf".source == modprobe;
    {
      toplevel = c.system.build.toplevel.drvPath;
      runtime = c.environment.etc."gpu-power-policy.json".source;
      inherit modprobe;
    };
  failures = map (a: a.message) (
    builtins.filter (a: !a.assertion) (makeSystem { open = false; }).config.assertions
  );
  results = map evaluate [
    {
      dynamicBoost = false;
      open = null;
    }
    {
      dynamicBoost = true;
      systemdInitrd = false;
    }
    {
      dynamicBoost = false;
      systemdInitrd = false;
    }
    {
      dynamicBoost = true;
      systemdInitrd = true;
    }
    {
      dynamicBoost = false;
      systemdInitrd = true;
    }
  ];
in
assert builtins.elem "gpuPowerPolicy requires hardware.nvidia.open." failures;
# Only evaluation results belong in this check; do not build the fake image/system.
pkgs.writeText "gpu-power-module-evaluation.json" (
  builtins.unsafeDiscardStringContext (builtins.toJSON { inherit results failures; })
)
