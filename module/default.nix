# NixOS integration for the pinned boot policy and powerd target filter.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.hardware.pt516GpuPower;
  base = config.boot.kernelPackages.nvidiaPackages.stable;
  # Mode 4 adds the current-limit envelope patch to the mode-3 driver.
  patched = base.open.overrideAttrs (old: {
    pname = if cfg.mode == 4 then "nvidia-open-pt516-envelope" else "nvidia-open-pt516-policy-125";
    patches =
      (old.patches or [ ])
      ++ [ ./policy-shim.patch ]
      ++ lib.optional (cfg.mode == 4) ./envelope-mode4.patch;
    makeFlags = old.makeFlags ++ [ "W=1" ];
  });
  nvml = "${base}/lib/libnvidia-ml.so.${base.version}";
  shim = pkgs.callPackage ./nvpcf { };
  runtime = pkgs.callPackage ./package.nix { inherit nvml; };
  launch = pkgs.writeShellScript "pt516-nvidia-powerd" ''
    exec ${pkgs.python3}/bin/python3 ${runtime}/share/pt516-gpu-power/launch.py \
      --powerd ${base.bin}/bin/nvidia-powerd \
      --shim ${shim}/lib/libnvpcf-shim.so --nvml ${nvml} \
      --python ${pkgs.python3}/bin/python3 --gdb ${pkgs.gdb}/bin/gdb \
      --journalctl ${pkgs.systemd}/bin/journalctl \
      --mode ${toString cfg.mode}
  '';
in
{
  options.hardware.pt516GpuPower = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable the pinned 125 W boot policy image and the nvidia-powerd launcher. Off until set explicitly.";
    };
    mode = lib.mkOption {
      type = lib.types.enum [
        3
        4
      ];
      default = 3;
      description = ''
        Boot-image mode. 3 is the 125 W image. 4 is the experiment
        envelope (policy 2 maximum 140 W, policy 13 maximum 120 A, rated values
        unchanged), which is what the runtime current-limit tool needs in order
        to raise policy 13 above 99 A. Because the rated values are untouched,
        mode 4 still boots at 99 A and a 125 W target; set it here for an
        ordinary entry, or leave this at 3 and use `envelopeSpecialisation`
        to keep mode 3 as the default entry.
      '';
    };
    captureBootVbios = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Retain the original boot VBIOS for optional offline inspection.";
    };
    envelopeSpecialisation = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Also build `specialisation.gpu-envelope`: the same system with
        mode 4, as an extra boot entry. Only needed if the default entry must
        keep the mode-3 driver; setting `mode = 4` above is simpler. Enter it
        with `bootctl set-oneshot`.
      '';
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        # Driver and NVML hooks are pinned together; see provenance.json.
        assertion =
          config.boot.kernelPackages.kernel.modDirVersion == "7.2.7" && base.version == "595.99.02";
        message = "gpu-power-policy requires NVIDIA 595.99.02 on Linux 7.2.7; update the patches, provenance.json and the NVML hooks together before changing that pin.";
      }
      {
        assertion = config.hardware.nvidia.open && config.hardware.nvidia.dynamicBoost.enable;
        message = "Predator GPU power requires the open NVIDIA module and nvidia-powerd.";
      }
    ];
    hardware.nvidia.package = lib.mkForce (base // { open = patched; });
    hardware.nvidia.moduleParams.nvidia = {
      NVreg_Pt516BootPolicy = cfg.mode;
      NVreg_CaptureBootVbios = if cfg.captureBootVbios then 1 else 0;
    };
    specialisation.gpu-envelope = lib.mkIf cfg.envelopeSpecialisation {
      configuration = {
        system.nixos.tags = [ "gpu-envelope" ];
        hardware.pt516GpuPower.mode = lib.mkForce 4;
        # Avoid recursively generating a specialisation inside itself.
        hardware.pt516GpuPower.envelopeSpecialisation = lib.mkForce false;
      };
    };
    systemd.services.nvidia-powerd = {
      after = [ "systemd-modules-load.service" ];
      serviceConfig = {
        ExecStartPre = [ "${pkgs.kmod}/bin/modprobe nvidia" ];
        ExecStart = lib.mkForce launch;
        TimeoutStartSec = 90;
        RuntimeDirectory = "pt516-gpu-power";
        RuntimeDirectoryMode = "0700";
        RuntimeDirectoryPreserve = "yes";
        # Discard any previous manual /run drop-in settings before running helpers.
        # The launcher supplies these only to powerd, after validating the policy.
        UnsetEnvironment = [
          "LD_PRELOAD"
          "LD_LIBRARY_PATH"
          "PYTHONPATH"
          "PYTHONHOME"
          "NVPCF_SHIM_TRACE_PMGR"
          "NVPCF_SHIM_KERNEL_LIMIT_MW"
          "NVPCF_SHIM_KERNEL_LIMIT_CAP_MW"
        ];
      };
    };
  };
}
