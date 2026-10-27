# Packages shared by the flake outputs and the NixOS module.
{
  pkgs,
  lib,
  vix-editor,
}:
let
  python = pkgs.python3;
  editorSource = vix-editor;
in
rec {
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
      $CC -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared -pthread -o libpowerd-shim.so powerd-shim.c -ldl
      $CC -std=c11 -Wall -Wextra -Werror -O1 -pthread -DSHIM_SOURCE="\"$PWD/powerd-shim.c\"" -o shim-test ${../tests/test_shim.c} -ldl
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
      ];
    };
    nativeBuildInputs = [ python ];
    dontBuild = true;
    doCheck = true;
    checkPhase = ''
      export PYTHONDONTWRITEBYTECODE=1
      ${python.interpreter} tests/test_rm.py
      ${python.interpreter} tests/test_cli.py
      ${python.interpreter} tests/test_make_image.py
    '';
    installPhase = ''
      mkdir -p $out/bin $out/lib/gpu-power
      cp src/rm.py src/policy.py $out/lib/gpu-power/
      cp src/gpu-power.py $out/bin/gpu-power
      sed -i "1s|.*|#!${python.interpreter}|; s|^sys.path.insert.*|sys.path.insert(0, \"$out/lib/gpu-power\")|" $out/bin/gpu-power
      chmod +x $out/bin/gpu-power
    '';
    meta.license = lib.licenses.mit;
  };

  # Panchovix's per-GPU override on NVIDIA's open module of the pinned driver set.
  patchedOpen =
    nvidia:
    assert nvidia.version == "615.71.09";
    nvidia.open.overrideAttrs (old: {
      pname = "nvidia-open-vbios-override";
      patches = (old.patches or [ ]) ++ [
        ../patches/vbios-override-615.71.09.patch
        ../patches/export-boot-vbios-615.71.09.patch
      ];
    });

  # Edit the user's own ROM with the upstream editor and export the loader payload.
  # Returns a directory laid out for hardware.firmware: lib/firmware/nvidia/<name>.
  policyImage =
    {
      rom,
      name,
      powerCeilingWatts,
      powerDefaultWatts,
      currentCeilingMilliamps,
      currentRatedMilliamps,
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
        runtimeJson = builtins.toJSON runtime;
        changes = builtins.toJSON {
          power_max_w = powerCeilingWatts;
          power_default_w = powerDefaultWatts;
          nvvdd_output_ocp_max_ma = currentCeilingMilliamps;
          nvvdd_output_ocp_rated_ma = currentRatedMilliamps;
        };
        passAsFile = [
          "changes"
          "runtimeJson"
        ];
        compressFirmware = false;
      }
      ''
        ${python.interpreter} ${../src/make-image.py} "$rom" "$changesPath" edited.rom payload.bin
        install -Dm444 payload.bin "$out/lib/firmware/nvidia/$firmwareName"
        cp payload.bin.json "$out/payload.json"
        cp edited.rom.json "$out/edit.json"
        ${python.interpreter} ${../src/image-config.py} "$runtimeJsonPath" payload.bin.json "$firmwareName" "$out"
      '';
}
