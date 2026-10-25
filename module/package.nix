# Runtime files are copied into the system closure; no /tmp or sibling checkout inputs.
{
  lib,
  stdenvNoCC,
  python3,
  gdb,
  binutils,
  nvml,
}:
stdenvNoCC.mkDerivation {
  pname = "pt516-gpu-power";
  version = "1";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./launch.py
      ./provenance.json
      ./check-maximum.py
      ./inspect-boot-log.py
      ./nvml-limit.py
      ./request
      ./tests
      ./policy-shim.patch
      ./envelope-mode4.patch
    ];
  };
  nativeBuildInputs = [
    python3
    gdb
    binutils
  ];
  dontConfigure = true;
  dontBuild = true;
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    export PYTHONDONTWRITEBYTECODE=1
    python3 -W error::ResourceWarning tests/test_startup.py
    python3 tests/test_setter.py
    python3 tests/test_image_edit.py
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/pt516-gpu-power
    cp -r launch.py provenance.json check-maximum.py inspect-boot-log.py nvml-limit.py request $out/share/pt516-gpu-power/
    substituteInPlace $out/share/pt516-gpu-power/{check-maximum.py,nvml-limit.py} \
      --replace-fail '@nvml@' '${nvml}'
    runHook postInstall
  '';
  meta = {
    description = "Predator PT516 125 W policy initialization (and one-shot envelope checks) for the pinned NVIDIA driver";
    platforms = [ "x86_64-linux" ];
    license = lib.licenses.mit;
  };
}
