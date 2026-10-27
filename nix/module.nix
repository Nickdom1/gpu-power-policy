{ vix-editor }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.hardware.gpuPowerPolicy;
  configured = cfg.vbios != null;
  # The editor, shim and CLI are stdlib Python and C: built from the consumer's nixpkgs.
  # Only the kernel module is tied to a driver build, and that comes from boot.kernelPackages.
  parts = import ./packages.nix { inherit pkgs lib vix-editor; };
  driver = config.boot.kernelPackages.nvidiaPackages.latest;
  name = "615.71.09/vbios-override/${cfg.device}.bin";
  image = parts.policyImage {
    rom = cfg.vbios;
    inherit name runtime;
    inherit (cfg)
      powerCeilingWatts
      powerDefaultWatts
      currentCeilingMilliamps
      currentRatedMilliamps
      ;
  };
  modprobeFile = pkgs.runCommand "gpu-power-policy-modprobe.conf" { } ''
    ln -s ${image}/modprobe.conf "$out"
  '';
  runtime = {
    inherit (cfg) device;
    modinfo = "${pkgs.kmod}/bin/modinfo";
    systemctl = "${pkgs.systemd}/bin/systemctl";
    journalctl = "${pkgs.systemd}/bin/journalctl";
    dmesg = "${pkgs.util-linux}/bin/dmesg";
    nvidiaSmi = "${driver.bin}/bin/nvidia-smi";
  }
  // lib.optionalAttrs configured {
    inherit (cfg)
      powerCeilingWatts
      powerDefaultWatts
      currentCeilingMilliamps
      currentRatedMilliamps
      powerdTargetWatts
      ;
  };
  positive =
    description:
    lib.mkOption {
      type = lib.types.nullOr (lib.types.ints.between 1 4294967);
      default = null;
      inherit description;
    };
in
{
  options.hardware.gpuPowerPolicy = {
    enable = lib.mkEnableOption "in-memory VBIOS power policy and runtime overrides for NVIDIA 615.71.09";
    device = lib.mkOption {
      type = lib.types.strMatching "[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\\.[0-7]";
      description = "GPU PCI BDF, explicitly selected for the image and runtime controls.";
      example = "0000:01:00.0";
    };
    vbios = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        A complete, saved VBIOS ROM from this GPU. Null enables bootstrap export
        with stock GPU behavior; obtain the ROM with gpu-power rom after reboot.
        The ROM is copied into the world-readable Nix store and edited offline;
        the patched driver stages the result in memory without flashing EEPROM.
        In a pure flake, use a path input or a hash-pinned builtins.fetchurl file URL.
      '';
    };
    powerCeilingWatts = positive "Board power ceiling in the image, in watts.";
    powerDefaultWatts = positive "Rated boot power in the image, in watts.";
    currentCeilingMilliamps = positive "NVVDD current ceiling in the image, in milliamps.";
    currentRatedMilliamps = positive "Rated boot NVVDD current, restored by current off, in milliamps.";
    powerdTargetWatts = lib.mkOption {
      type = lib.types.nullOr (lib.types.ints.between 1 4294967);
      default = null;
      description = "Initial power target in watts, via Dynamic Boost when available or a direct limit request; null keeps stock behavior.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = driver.version == "615.71.09";
        message = "gpuPowerPolicy requires NVIDIA 615.71.09 from the selected kernel package set.";
      }
      {
        assertion = config.hardware.nvidia.open == true;
        message = "gpuPowerPolicy requires hardware.nvidia.open.";
      }
      {
        assertion =
          !configured
          || (
            builtins.all (v: v != null) [
              cfg.powerDefaultWatts
              cfg.powerCeilingWatts
              cfg.currentRatedMilliamps
              cfg.currentCeilingMilliamps
            ]
            && cfg.powerDefaultWatts <= cfg.powerCeilingWatts
            && cfg.currentRatedMilliamps <= cfg.currentCeilingMilliamps
          );
        message = "gpuPowerPolicy configured images require all four limits, with rated/default values at most the ceilings.";
      }
      {
        assertion =
          cfg.powerdTargetWatts == null
          || (configured && cfg.powerCeilingWatts != null && cfg.powerdTargetWatts <= cfg.powerCeilingWatts);
        message = "gpuPowerPolicy powerd target requires a configured image and must not exceed its ceiling.";
      }
    ];
    hardware = {
      nvidia = {
        # The pinned upstream module also treats its nullable default as a Boolean.
        open = lib.mkDefault true;
        package = driver // {
          open = parts.patchedOpen driver;
        };
      };
      firmware = lib.optionals configured [ image ];
    };
    environment = {
      systemPackages = [ parts.gpu-power ];
      etc."gpu-power-policy.json".source =
        if configured then
          "${image}/runtime.json"
        else
          pkgs.writeText "gpu-power-policy.json" (builtins.toJSON runtime);
      etc."modprobe.d/gpu-power-policy.conf" = lib.mkIf configured {
        source = modprobeFile;
      };
    };
    # Both initrd implementations must receive the generated options before NVIDIA loads.
    boot.initrd = lib.mkIf configured {
      extraFiles."etc/modprobe.d/gpu-power-policy.conf".source = modprobeFile;
      systemd.contents."/etc/modprobe.d/gpu-power-policy.conf".source = modprobeFile;
    };
    systemd = {
      tmpfiles.rules = [ "d /run/gpu-power-policy 0755 root root -" ];
      services.nvidia-powerd = lib.mkIf (configured && config.hardware.nvidia.dynamicBoost.enable) {
        environment = {
          LD_PRELOAD = "${parts.powerd-shim}/lib/libpowerd-shim.so";
          GPU_POWER_DEVICE = cfg.device;
          GPU_POWER_CEILING_MW = toString (cfg.powerCeilingWatts * 1000);
        };
        requires = [ "gpu-power-policy-init.service" ];
        after = [ "gpu-power-policy-init.service" ];
      };
      services.gpu-power-policy-init = {
        description = "Initialize the selected GPU image's boot power target";
        wantedBy = [ "multi-user.target" ];
        before = lib.optional config.hardware.nvidia.dynamicBoost.enable "nvidia-powerd.service";
        after = [
          "systemd-tmpfiles-setup.service"
          "systemd-modules-load.service"
        ];
        # Runtime on/off must survive powerd restarts and rebuilds within this boot.
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script = ''
          exec ${parts.gpu-power}/bin/gpu-power boot-init
        '';
      };
    };
  };
}
