# Known gaps

The [README Results](../README.md#results) cover one laptop. Other boards, sustained raised operation, games, suspend/resume, battery transitions and multi-GPU systems have no end-to-end validation here.

- The runtime ABI is pinned to NVIDIA 615.78.08; other versions are refused. Live confirmation and fixture recapture are pending; the recorded GET fixtures and Dynamic Boost E634 layout still come from 615.71.09.
- Ada current control and Ada laptop image raises are unavailable. Ada desktop power raising is experimental.
- The target override replaces Dynamic Boost CPU/GPU and adapter arbitration and also applies on battery. There is no AC or thermal watchdog; regulator temperature is not measured.
- Current SET verifies readback but does not automatically restore the preceding value after a mismatched readback. Run `sudo gpu-power off` and inspect status.
- Firmware arbitration status A619 is captured by `dump` but is not decoded to identify the binding limiter.
- CPU tests cannot establish Secure Boot enrollment, a working desktop after recovery, or hardware safety. Each release needs its documented hardware and distribution lifecycle checks; older VM evidence is not validation of a newer flow.
- CachyOS updates replace kernels in place. Its LTS kernel is an explicit fallback, not a guarantee of automatic rollback.
- The CLI targets x86_64 Linux; there is no GUI or Windows implementation here.
