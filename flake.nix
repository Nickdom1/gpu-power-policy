{
  description = "In-memory VBIOS power-policy edit, nvidia-powerd target override and runtime current control for NVIDIA 615.71.09 laptops";

  inputs = {
    # Same revision as the host's nixpkgs-kernel input: Linux 7.2.7 and NVIDIA 615.71.09.
    nixpkgs.url = "github:nixos/nixpkgs/58a20b0a882fffd508ca4dad73836d76632792a3";
    vix-editor = {
      url = "github:Panchovix/nvidia-vbios-vix-editor/edf0f483d8d4a3d7a578f941647c95b503ab9795";
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
      parts = import ./nix/packages.nix { inherit pkgs lib vix-editor; };
    in
    {
      nixosModules.default = import ./nix/module.nix { inherit vix-editor; };

      packages.${system} = {
        inherit (parts) vix-editor powerd-shim gpu-power;
        # The patched 615.71.09 open kernel module for this nixpkgs' linuxPackages_latest.
        kernel-module = parts.patchedOpen pkgs.linuxPackages_latest.nvidiaPackages.latest;
        default = parts.gpu-power;
      };

      # CPU-only checks. The driver build is `nix build .#kernel-module` (unfree, minutes).
      checks.${system} = {
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
