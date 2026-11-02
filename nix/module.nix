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
  name = "vbios-override/${if cfg.device == null then "unselected" else cfg.device}.bin";
  image = parts.policyImage {
    rom = cfg.vbios;
    inherit name runtime;
    inherit (cfg)
      powerCeilingWatts
      currentCeilingMilliamps
      beyondTested
      ;
  };
  modprobeFile = pkgs.runCommand "gpu-power-policy-modprobe.conf" { } ''
    ln -s ${image}/modprobe.conf "$out"
  '';
  runtime = {
    inherit (cfg) device powerdTargetWatts;
    dynamicBoostEnabled = config.hardware.nvidia.dynamicBoost.enable;
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
  _file = "gpu-power-policy/nix/module.nix";
  key = "gpu-power-policy";
  options.hardware.gpuPowerPolicy = {
    enable = lib.mkEnableOption "in-memory VBIOS power policy and runtime overrides for NVIDIA ${parts.pins.version}";
    device = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\\.[0-7]");
      default = null;
      description = "GPU PCI BDF. Null auto-selects the only NVIDIA display GPU in the stock install; configure prints the selected address.";
      example = "0000:01:00.0";
    };
    vbios = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        A complete, saved VBIOS ROM from this GPU. Null enables the stock install and ROM export; obtain the ROM with gpu-power rom after reboot.
        The ROM is copied into the world-readable Nix store and edited offline;
        the patched driver stages the result in memory without flashing EEPROM.
        gpu-power configure --nix emits an importable module with a hash-pinned file URL.
      '';
    };
    powerCeilingWatts = positive "Board power ceiling in the image, in watts.";
    currentCeilingMilliamps = positive "NVVDD current ceiling in the image, in milliamps.";
    beyondTested = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Permit ceilings beyond the +10% ROM maximum cap; does not establish hardware safety.";
    };
    powerdTargetWatts = lib.mkOption {
      type = lib.types.nullOr (
        lib.types.oneOf [
          (lib.types.enum [ "max" ])
          (lib.types.ints.between 1 4294967)
        ]
      );
      default = null;
      description = "Initial power target in watts, via Dynamic Boost when available or a direct limit request; null keeps stock behavior.";
    };
  };

  config = lib.mkIf cfg.enable {
    specialisation.stock-vbios = lib.mkIf configured {
      configuration.hardware.gpuPowerPolicy.vbios = lib.mkForce null;
    };
    # This activates NixOS's NVIDIA module/package wiring for the two-line flow.
    services.xserver.videoDrivers = [ "nvidia" ];
    assertions = [
      {
        assertion = driver.version == parts.pins.version;
        message = ''
          gpuPowerPolicy needs NVIDIA ${parts.pins.version}; boot.kernelPackages provides ${driver.version}.
          Add: boot.kernelPackages = inputs.gpu-power-policy.kernelPackages.x86_64-linux;
          or choose a kernelPackages whose nvidiaPackages.latest is ${parts.pins.version}.
        '';
      }
      {
        assertion = config.hardware.nvidia.open == true;
        message = "gpuPowerPolicy requires hardware.nvidia.open.";
      }
      {
        assertion = !configured || cfg.device != null;
        message = "gpuPowerPolicy configured images require an explicit device; use gpu-power configure --nix.";
      }
      {
        assertion =
          cfg.powerdTargetWatts == null
          || cfg.powerdTargetWatts == "max"
          || cfg.powerCeilingWatts == null
          || cfg.powerdTargetWatts <= cfg.powerCeilingWatts;
        message = "gpuPowerPolicy powerd target must not exceed its configured ceiling; live bounds are checked at boot.";
      }
    ];
    hardware = {
      nvidia = {
        # The pinned upstream module also treats its nullable default as a Boolean.
        open = lib.mkDefault true;
        dynamicBoost.enable = lib.mkDefault true;
        package = driver.overrideAttrs (old: {
          passthru = old.passthru // {
            # Let the friendly module assertion report a mismatched driver first.
            open = if driver.version == parts.pins.version then parts.patchedOpen driver else driver.open;
          };
        });
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
      interactiveShellInit = ''
        if [ -r /run/gpu-power-policy/notice ]; then cat /run/gpu-power-policy/notice; fi
      '';
      etc."modprobe.d/gpu-power-policy.conf" = lib.mkIf configured {
        source = modprobeFile;
      };
    };
    # Both initrd implementations must receive the generated options before NVIDIA loads.
    boot.initrd = lib.mkIf configured {
      extraFirmwarePaths = [ "nvidia/${name}" ];
      extraFiles."etc/modprobe.d/gpu-power-policy.conf".source = modprobeFile;
      systemd.contents."/etc/modprobe.d/gpu-power-policy.conf".source = modprobeFile;
    };
    systemd = {
      tmpfiles.rules = [ "d /run/gpu-power-policy 0755 root root -" ];
      services.nvidia-powerd = lib.mkIf config.hardware.nvidia.dynamicBoost.enable {
        environment = {
          LD_PRELOAD = "${parts.powerd-shim}/lib/libpowerd-shim.so";
        };
        requires = [ "gpu-power-policy-init.service" ];
        after = [ "gpu-power-policy-init.service" ];
        serviceConfig.ExecCondition = pkgs.writeShellScript "gpu-power-dynamic-boost-supported" ''
          export PATH=${lib.makeBinPath [ pkgs.gnugrep pkgs.coreutils ]}
          ${builtins.readFile ../src/powerd-supported.sh}
        '';
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
        path = [ config.hardware.nvidia.package.bin ];
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
