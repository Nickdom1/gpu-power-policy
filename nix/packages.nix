# Packages shared by the flake outputs and the NixOS module.
{
  pkgs,
  lib,
  vix-editor,
}:
let
  python = pkgs.python3;
  editorSource = vix-editor;
  pins = import ./pins.nix lib;
in
assert lib.assertMsg (vix-editor.rev == pins.editor_commit) "The locked editor revision differs from install/pins.sh";
rec {
  inherit pins;
  # Panchovix's offline editor, unmodified (GPL-3.0-or-later, stdlib only).
  vix-editor = python.pkgs.buildPythonApplication {
    pname = "nvidia-vbios-vix-editor";
    version = "0.1.35";
    src = editorSource;
    pyproject = true;
    build-system = [ python.pkgs.setuptools ];
    doCheck = true;
    # The GTK window is not packaged; its tests are skipped by the suite when gi is absent.
    installCheckPhase = ''
      cd $src && PYTHONPATH=src ${python.interpreter} -m unittest discover -s tests >/dev/null
    '';
    meta.license = lib.licenses.gpl3Plus;
  };

  powerd-shim = pkgs.stdenv.mkDerivation {
    pname = "gpu-power-powerd-shim";
    version = "1";
    src = ../src;
    dontConfigure = true;
    buildPhase = ''
      $CC -DGPU_POWER_DRIVER_VERSION='"${pins.version}"' -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared -pthread -o libpowerd-shim.so powerd-shim.c -ldl
      $CC -DGPU_POWER_DRIVER_VERSION='"${pins.version}"' -std=c11 -Wall -Wextra -Werror -O1 -pthread -DSHIM_SOURCE="\"$PWD/powerd-shim.c\"" -o shim-test ${../tests/test_shim.c} -ldl
    '';
    doCheck = true;
    checkPhase = "./shim-test";
    installPhase = "install -Dm755 libpowerd-shim.so $out/lib/libpowerd-shim.so";
    meta.license = lib.licenses.mit;
  };

  gpu-power = pkgs.stdenvNoCC.mkDerivation {
    pname = "gpu-power";
    version = "1";
    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.unions [
        ../src
        ../tests
        ../install
        ../flake.lock
      ];
    };
    nativeBuildInputs = [ python pkgs.makeWrapper ];
    dontBuild = true;
    doCheck = true;
    checkPhase = ''
      export PYTHONDONTWRITEBYTECODE=1
      ${python.interpreter} -m unittest discover -s tests -v
    '';
    installPhase = ''
      mkdir -p $out/bin $out/lib/gpu-power
      cp src/rm.py src/policy.py src/baseline.py $out/lib/gpu-power/
      cp src/gpu-power.py $out/bin/gpu-power
      sed -i "1s|.*|#!${python.interpreter}|; s|^sys.path.insert.*|sys.path.insert(0, \"$out/lib/gpu-power\")|" $out/bin/gpu-power
      chmod +x $out/bin/gpu-power
      wrapProgram $out/bin/gpu-power --prefix PATH : ${lib.makeBinPath [ pkgs.kmod pkgs.systemd pkgs.util-linux vix-editor ]}
    '';
    meta.license = lib.licenses.mit;
  };

  preflight = pkgs.writeShellApplication {
    name = "gpu-power-preflight";
    runtimeInputs = [
      python
      pkgs.pciutils
      pkgs.kmod
      pkgs.systemd
    ];
    text = ''
      exec python3 ${../install}/preflight.py "$@"
    '';
  };

  # Panchovix's per-GPU override on NVIDIA's open module of the pinned driver set.
  patchedOpen =
    nvidia:
    if nvidia.version != pins.version then
      throw "gpu-power-policy requires NVIDIA ${pins.version}; got ${nvidia.version}"
    else
      nvidia.open.overrideAttrs (old: {
        pname = "nvidia-open-vbios-override";
        patches = (old.patches or [ ]) ++ map (name: ../patches + "/${name}")
          (lib.filter (name: name != "") (lib.splitString "\n" (builtins.readFile ../patches/series)));
      });

  # Edit the user's own ROM with the upstream editor and export the loader payload.
  # Returns a directory laid out for hardware.firmware: lib/firmware/nvidia/<name>.
  policyImage =
    {
      rom,
      name,
      powerCeilingWatts ? null,
      currentCeilingMilliamps ? null,
      beyondTested ? false,
      runtime,
    }:
    pkgs.runCommand "gpu-power-vbios-${builtins.baseNameOf name}"
      {
        nativeBuildInputs = [
          vix-editor
          python
        ];
        inherit rom;
        firmwareName = name;
        request = builtins.toJSON (runtime // lib.filterAttrs (_: value: value != null) {
          inherit powerCeilingWatts currentCeilingMilliamps beyondTested;
        });
        passAsFile = [ "request" ];
        compressFirmware = false;
      }
      ''
        ${python.interpreter} ${../src}/make-image.py "$rom" "$requestPath" "$out"
        install -Dm444 "$out/payload.bin" "$out/lib/firmware/nvidia/$firmwareName"
        cp "$out/payload.bin.json" "$out/payload.json"
        cp "$out/edited.rom.json" "$out/edit.json"
      '';
}
