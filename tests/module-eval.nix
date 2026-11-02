{
  nixpkgs,
  module,
  pkgs,
}:
let
  inherit (nixpkgs) lib;
  makeSystem =
    {
      enabled ? true,
      dynamicBoost ? false,
      systemdInitrd ? false,
      open ? true,
      bootstrap ? false,
      bootPower ? null,
      wrongDriver ? false,
    }:
    lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        module
        ({ pkgs, ... }: {
          system.stateVersion = "24.11";
          nixpkgs.config.allowUnfree = true;
          boot = {
            kernelPackages =
              if wrongDriver then
                pkgs.linuxPackages_latest.extend (
                  _: previous: {
                    nvidiaPackages = previous.nvidiaPackages // {
                      latest = previous.nvidiaPackages.latest // {
                        version = "wrong-version";
                      };
                    };
                  }
                )
              else
                pkgs.linuxPackages_latest;
            loader.grub.enable = false;
            initrd.systemd.enable = systemdInitrd;
          };
          fileSystems."/" = {
            device = "/dev/vda1";
            fsType = "ext4";
          };
          hardware = {
            nvidia.open = lib.mkIf (open != null) open;
            nvidia.dynamicBoost.enable = dynamicBoost;
            gpuPowerPolicy = {
              enable = enabled;
              powerdTargetWatts = bootPower;
            }
            // lib.optionalAttrs (!bootstrap) {
              device = "0000:01:00.0";
              # Evaluation must not attempt to build or read a policy image.
              vbios = builtins.toFile "evaluation-only.rom" "not a ROM";
              powerCeilingWatts = 140;
              beyondTested = true;
              currentCeilingMilliamps = 120000;
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
      stock = c.specialisation.stock-vbios.configuration;
      modprobe = c.environment.etc."modprobe.d/gpu-power-policy.conf".source;
    in
    assert builtins.all (a: a.assertion) c.assertions;
    assert c.specialisation ? stock-vbios;
    assert stock.hardware.gpuPowerPolicy.vbios == null;
    assert stock.boot.kernelPackages.kernel.drvPath == c.boot.kernelPackages.kernel.drvPath;
    assert stock.hardware.nvidia.package.open.drvPath == c.hardware.nvidia.package.open.drvPath;
    assert builtins.any (p: (p.pname or "") == "gpu-power") stock.environment.systemPackages;
    assert !(stock.environment.etc ? "modprobe.d/gpu-power-policy.conf");
    assert !(lib.elem "nvidia/vbios-override/0000:01:00.0.bin" stock.boot.initrd.extraFirmwarePaths);
    assert c.systemd.services ? nvidia-powerd == settings.dynamicBoost;
    assert c.boot.initrd.extraFiles."etc/modprobe.d/gpu-power-policy.conf".source == modprobe;
    assert c.boot.initrd.systemd.contents."/etc/modprobe.d/gpu-power-policy.conf".source == modprobe;
    assert builtins.elem "nvidia/vbios-override/0000:01:00.0.bin"
      c.boot.initrd.extraFirmwarePaths;
    {
      # One full system catches integration failures; other cases force assertions
      # and module outputs without retaining six complete system closures at once.
      toplevel =
        if settings ? open && settings.open == null then c.system.build.toplevel.drvPath else null;
      runtime = c.environment.etc."gpu-power-policy.json".source;
      inherit modprobe;
    };
  bootstrap =
    (makeSystem {
      bootstrap = true;
      open = null;
      dynamicBoost = true;
      bootPower = "max";
    }).config;
  # Keeping the generated raised settings must not prevent host-level disable.
  disabled = (makeSystem { enabled = false; }).config;
  failures = map (a: a.message) (
    builtins.filter (a: !a.assertion) (makeSystem { open = false; }).config.assertions
  );
  versionFailures = map (a: a.message) (
    builtins.filter (a: !a.assertion)
      (makeSystem {
        wrongDriver = true;
        bootstrap = true;
      }).config.assertions
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
assert disabled.hardware.gpuPowerPolicy.vbios != null;
assert !(disabled.environment.etc ? "gpu-power-policy.json");
assert !(disabled.environment.etc ? "modprobe.d/gpu-power-policy.conf");
assert !(disabled.systemd.services ? gpu-power-policy-init);
assert !(disabled.specialisation ? stock-vbios);
assert !(builtins.any (p: (p.pname or "") == "gpu-power") disabled.environment.systemPackages);
assert builtins.all (a: a.assertion) disabled.assertions;
assert builtins.elem "nvidia" bootstrap.services.xserver.videoDrivers;
assert builtins.any (
  p: p.outPath == bootstrap.hardware.nvidia.package.open.outPath
) bootstrap.boot.extraModulePackages;
assert bootstrap.hardware.nvidia.package.drvPath == bootstrap.boot.kernelPackages.nvidiaPackages.latest.drvPath;
assert (bootstrap.hardware.nvidia.package.override { libsOnly = true; }).open.pname == "nvidia-open-vbios-override";
assert bootstrap.hardware.nvidia.package.persistenced == bootstrap.boot.kernelPackages.nvidiaPackages.latest.persistenced;
assert !(bootstrap.specialisation ? stock-vbios);
assert bootstrap.hardware.gpuPowerPolicy.device == null;
assert !(bootstrap.environment.etc ? "profile.d/gpu-power-policy.sh");
assert lib.hasInfix "/run/gpu-power-policy/notice" bootstrap.environment.interactiveShellInit;
assert builtins.elem bootstrap.hardware.nvidia.package.bin bootstrap.systemd.services.gpu-power-policy-init.path;
assert builtins.all (a: a.assertion) bootstrap.assertions;
assert bootstrap.hardware.gpuPowerPolicy.powerdTargetWatts == "max";
assert bootstrap.systemd.services.nvidia-powerd.environment ? LD_PRELOAD;
assert !(bootstrap.systemd.services.nvidia-powerd.environment ? GPU_POWER_CEILING_MW);
assert builtins.any (
  s: lib.hasInfix "boot.kernelPackages = inputs.gpu-power-policy.kernelPackages.x86_64-linux" s
) versionFailures;
assert builtins.elem "gpuPowerPolicy requires hardware.nvidia.open." failures;
# Only evaluation results belong in this check; do not build the fake image/system.
pkgs.writeText "gpu-power-module-evaluation.json" (
  builtins.unsafeDiscardStringContext (
    builtins.toJSON {
      inherit results failures;
      bootstrap = bootstrap.environment.etc."gpu-power-policy.json".source;
    }
  )
)
