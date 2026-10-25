# Pinned environment for CPU checks. Usage: nix-shell --run './lint.sh && ./test.sh'
let
  pkgs = import (fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/ecc58f32d1069a3e3f8a991e2233d365689f748d.tar.gz";
    sha256 = "sha256-vrwAiXmz+0hs/IWXZalFG/Ws6xQRcW+gClUDlfvcfjQ=";
  }) { };
in
pkgs.mkShell {
  packages = [
    (pkgs.python3.withPackages (p: [ p.pytest ]))
    pkgs.gdb
    pkgs.binutils
    pkgs.gcc
    pkgs.shellcheck
    pkgs.ruff
    pkgs.deadnix
    pkgs.nixfmt
    pkgs.actionlint
  ];
}
