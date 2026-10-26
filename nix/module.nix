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
    inherit name;
    inherit (cfg)
      powerCeilingWatts
      powerDefaultWatts
      currentCeilingMilliamps
      currentRatedMilliamps
      ;
  };
  imageInfo = builtins.fromJSON (builtins.readFile "${image}/payload.json");
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
      description = "Initial powerd target override in watts; null leaves stock powerd targets. Runtime changes last until reboot.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = driver.version == "615.71.09";
        message = "gpuPowerPolicy requires NVIDIA 615.71.09 from the selected kernel package set.";
      }
      {
        assertion = config.hardware.nvidia.open && config.hardware.nvidia.dynamicBoost.enable;
        message = "gpuPowerPolicy requires hardware.nvidia.open and dynamicBoost.enable.";
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
      nvidia.package = driver // {
        open = parts.patchedOpen driver;
      };
      firmware = lib.optionals configured [ image ];
      nvidia.moduleParams.nvidia = lib.optionalAttrs configured {
        NVreg_RegistryDwordsPerDevice = ''"pci=${cfg.device};RmLinuxVbiosOverride=2;RmLinuxVbiosOverrideSize=${toString imageInfo.output_size}"'';
        NVreg_RegistryBinaryFilePerDevice = ''"pci=${cfg.device};RmLinuxVbiosOverrideImage=${name}"'';
      };
    };
    environment.systemPackages = [ parts.gpu-power ];
    environment.etc."gpu-power-policy.json".text = builtins.toJSON (
      {
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
          ;
        inherit (imageInfo) output_sha256 output_size;
      }
    );
    systemd = lib.mkIf configured {
      tmpfiles.rules = [ "d /run/gpu-power-policy 0755 root root -" ];
      services.nvidia-powerd = {
        environment = {
          LD_PRELOAD = "${parts.powerd-shim}/lib/libpowerd-shim.so";
          GPU_POWER_DEVICE = cfg.device;
          GPU_POWER_CEILING_MW = toString (cfg.powerCeilingWatts * 1000);
        };
        requires = [ "gpu-power-policy-init.service" ];
        after = [ "gpu-power-policy-init.service" ];
      };
      services.gpu-power-policy-init = {
        description = "Initialize the boot powerd target override";
        before = [ "nvidia-powerd.service" ];
        after = [ "systemd-tmpfiles-setup.service" ];
        # Runtime on/off must survive powerd restarts and rebuilds within this boot.
        restartIfChanged = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        script =
          lib.optionalString (cfg.powerdTargetWatts != null) ''
            echo ${toString (cfg.powerdTargetWatts * 1000)} > /run/gpu-power-policy/target-mw
          ''
          + lib.optionalString (cfg.powerdTargetWatts == null) ''
            rm -f /run/gpu-power-policy/target-mw
          '';
      };
    };
  };
}
