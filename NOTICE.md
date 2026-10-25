# NVIDIA patch and build notices

Patches: `module/policy-shim.patch` and `module/envelope-mode4.patch` modify files
of NVIDIA's open-gpu-kernel-modules (commit `e394a404`), each of which upstream
licenses under the MIT License, Copyright (c) NVIDIA CORPORATION & AFFILIATES.
The hunks and the new files `nv-boot-policy.c` and `nv-boot-vbios.c` are
Copyright (c) 2026 Nicholas DeTello, MIT License. Context lines reproduced in
the patches remain NVIDIA's under its notice. A kernel module built from the
patched tree is, like upstream, dual-licensed MIT / GPL-2.0.

The project's MIT license is in [LICENSE](LICENSE).
