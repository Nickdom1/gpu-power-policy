# LD_PRELOAD library for NVIDIA's Dynamic Boost daemon (nvidia-powerd). It
# replaces the board-power target the daemon sends. See nvpcf-shim.c for the pinned 595.99.02 control layouts and guards.
{ lib, stdenv }:

stdenv.mkDerivation {
  pname = "nvpcf-shim";
  version = "0.1";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./nvpcf-shim.c
      ./test-runtime.c
      ./LICENSE
    ];
  };
  dontConfigure = true;
  buildPhase = ''
    runHook preBuild
    $CC -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared -o libnvpcf-shim.so nvpcf-shim.c -ldl
    runHook postBuild
  '';
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    $CC -std=gnu11 -Wall -Wextra -Werror -O1 -o test-runtime test-runtime.c
    ./test-runtime
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    install -Dm755 libnvpcf-shim.so $out/lib/libnvpcf-shim.so
    install -Dm644 LICENSE $out/share/licenses/nvpcf-shim/LICENSE
    runHook postInstall
  '';
  meta = {
    description = "Replace the nvidia-powerd target (PT516-52s experiment)";
    license = lib.licenses.mit;
    platforms = [ "x86_64-linux" ];
  };
}
