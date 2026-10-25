# Policy-status inspection

This independent, read-only diagnostic captures firmware inventory (RM
`0x2080A618`) and status (`0x2080A619`). It can capture an unexpected current
state and then report the seed verdict. It does not require a 99 A control
readback before capturing status.

From the repository root, after the patched driver and `nvidia-powerd` are
active, use a desktop terminal on AC with the GPU otherwise idle:

```sh
sudo bash tools/policy-status/run.sh --idle-only
```

See the [tool guide](../README.md#prepare) for dependencies. To preview the
operation without hardware access:

```sh
bash tools/policy-status/run.sh --plan
```

Inspect `SEED CHECK` in the summary or `seed-check.json` in the printed
`Saved: /var/tmp/...` directory. Mode 4 must report `PASS`; mode 3 must report
`stock seeds confirmed`. **Capture completion and exit status 0 do not establish
a passing seed verdict.** If the verdict fails, says `unexpected idle state`,
or is missing, stop before the smoke test or any current change. Retain the
artifacts and investigate the reported state; use the
[recovery guide](../../SAFETY.md#levels-of-undo) if needed. On an identity or
capture refusal, check the running configuration and reported error rather
than bypassing the guard.

`codec.py`, `capture.gdb` and `bootstrap.py` implement the pinned GET transport
and decoder. Numerical matches between offered and arbitrated clocks do not
establish a unique binding limiter. `series.py`, `witness.py` and
`xclient_pid.py` also supply placement checks for the
[current-limit experiments](../current-limit/README.md).
