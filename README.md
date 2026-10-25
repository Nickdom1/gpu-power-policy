# GPU Power Policy

Experimental Linux tools for power and current limits on the Acer Predator Triton
500 SE (PT516-52s), RTX 3080 Ti Laptop GPU. Requires NVIDIA 595.99.02 with GSP
and Linux 7.2.7. Other configurations are unsupported.

## How it works

At boot, a patched NVIDIA open kernel module edits the VBIOS power-policy table
in RAM; nothing is flashed. A launcher checks compatibility, requests the higher
board-power limit and starts `nvidia-powerd` with a filter that sets its target
to 125 W.

At runtime, the experiment client changes the separate current limit, checks
readback and attempts restoration on exit. Mode 4 permits settings above 99 A
while still starting at 99 A. The client runs bounded experiments; it does not
provide a persistent current-setting service.

## Results on the reference laptop

Raising the board-power target from 120 to 125 W left FurMark near 118 W.
Lowering and raising the current limit tested the separate constraint:

| Change | Observation |
|---|---|
| Boot policy edit and powerd filter | Board-power maximum raised from 115 to 125 W; runtime target set to 125 W |
| Current limit 99 → 95 A | SM clocks fell 7.4–8.9% in four pulses, then recovered after restoration |
| Current limit 99 → 107 A, target held at 125 W | **+6.6% FurMark score**, two runs per condition at 99, 107, 107 and 99 A (ABBA) |

The configured maximum is distinct from measured consumption. These historical
results do not establish gaming gains or a safe sustained regulator limit.
See [measurements and conditions](docs/results.md),
[research history](docs/history.md) and [related work](docs/prior-art.md).

## Install and check

Benchmarks were run on the reference laptop. Subsequent tool changes have
passed automated tests but have not been rerun on hardware.

**Use on AC under supervision.** The installed filter does not handle battery
operation. A rejected boot image can prevent GPU initialization until the edit
is disabled. Read [safety and recovery](SAFETY.md),
[known issues](CHANGELOG.md#known-issues) and the
[validation status](PROVENANCE.md#validation) before installing.

- [Install on NixOS](docs/install-nixos.md)
- [Other distributions: untested porting guidance](docs/install-other-distros.md)
- [Check the setup and run experiments](tools/README.md)
- [Boot patch and launcher reference](module/README.md)

From the repository root, run the CPU checks without accessing the GPU:

```sh
nix-shell --option max-jobs 1 --option cores 2 --run './lint.sh && ./test.sh'
```

For an unsupported board, submit a [stock-system report](.github/ISSUE_TEMPLATE/board-report.md).
No patched driver or firmware upload is needed.

## License

[MIT](LICENSE), with NVIDIA patch/build terms in [NOTICE.md](NOTICE.md).
No NVIDIA firmware, VBIOS images or board schematics are distributed here.
