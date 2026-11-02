{
  description = "In-memory VBIOS power-policy edit, nvidia-powerd target override and runtime current control for NVIDIA 615.78.08 laptops";

  inputs = {
    # Pinned compatible kernel set: Linux 7.2.11 and NVIDIA 615.78.08.
    nixpkgs.url = "github:nixos/nixpkgs/09063bd1f2b36fd9fd793c36dfade7bd40a93dfc";
    vix-editor = {
      url = "github:Panchovix/nvidia-vbios-vix-editor";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      vix-editor,
    }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };
      inherit (pkgs) lib;
      # 0.19.2 corrupts NVIDIA ELF load mappings (NixOS/patchelf#672).
      patchelf = pkgs.patchelf.overrideAttrs (final: _: {
        version = "0.19.3";
        src = pkgs.fetchurl {
          url = "https://github.com/NixOS/patchelf/releases/download/${final.version}/patchelf-${final.version}.tar.bz2";
          hash = "sha256-INviPp28StdpB4qGetACwEKXoGn/k8lftyJC3VXU3W8=";
        };
      });
      kernelPackages = pkgs.linuxPackages_latest.extend (_: previous: {
        nvidiaPackages = previous.nvidiaPackages // lib.genAttrs [ "latest" "production" "stable" ]
          (name: previous.nvidiaPackages.${name}.overrideAttrs (old: {
            nativeBuildInputs = [ patchelf ] ++ old.nativeBuildInputs;
          }));
      });
      parts = import ./nix/packages.nix { inherit pkgs lib vix-editor; };
    in
    {
      kernelPackages.${system} = kernelPackages;
      apps.${system}.preflight = {
        type = "app";
        program = "${parts.preflight}/bin/gpu-power-preflight";
      };
      nixosModules.default = import ./nix/module.nix { inherit vix-editor; };

      packages.${system} = {
        inherit (parts)
          vix-editor
          powerd-shim
          gpu-power
          preflight
          ;
        # The patched 615.78.08 open kernel module for this nixpkgs' linuxPackages_latest.
        kernel-module = parts.patchedOpen kernelPackages.nvidiaPackages.latest;
        default = parts.gpu-power;
      };

      # CPU-only checks. The driver build is `nix build .#kernel-module` (unfree, minutes).
      checks.${system} = {
        nvidia-userspace = import ./tests/nvidia-userspace.nix { inherit pkgs kernelPackages; };
        module-eval = import ./tests/module-eval.nix {
          inherit nixpkgs pkgs;
          module = self.nixosModules.default;
        };
        installer-tests =
          pkgs.runCommand "gpu-power-installer-tests"
            {
              nativeBuildInputs = [
                pkgs.python3
                pkgs.bash
                pkgs.coreutils
              ];
              src = ./.;
            }
            ''
              export PYTHONDONTWRITEBYTECODE=1
              python3 $src/tests/test_install.py
              touch $out
            '';
        inherit (self.packages.${system})
          powerd-shim
          gpu-power
          vix-editor
          ;
        # Image edit on a synthetic ROM exercising the real editor and the module's derivation.
        edit-tests =
          pkgs.runCommand "gpu-power-edit-tests"
            {
              nativeBuildInputs = [
                pkgs.python3
                parts.vix-editor
              ];
              src = ./.;
              EDITOR_TESTS = "${vix-editor}/tests";
              PYTHONPATH = "${vix-editor}/src";
            }
            ''
              export PYTHONDONTWRITEBYTECODE=1
              python3 $src/tests/test_edit.py
              touch $out
            '';
      };
    };
}
