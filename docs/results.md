# Results

All runs: Acer PT516-52s, RTX 3080 Ti Laptop GPU, NVIDIA 595.99.02 open module, Linux 7.2.7, AC power, external cooler, FurMark 2.10.2 (Vulkan, 2560×1440, fullscreen) on the laptop panel unless stated. Per-run summaries are in [results/](results/); raw telemetry and control-request captures are not published.

## Board power: 125 W (October 2026)

Stock Linux granted 80 W at idle and about 112 W under load against a 115 W VBIOS maximum. With the in-RAM policy edit, the role-8 request and the powerd target filter, the GPU reports a 125 W maximum and target. Panel FurMark plateaus at 117.4–118.9 W mean GPU power and 750–772 MHz median SM clock across the published ladder’s 99 A reference windows, 89 FPS. Raising the target from 120 to 125 W changed nothing: something else was binding.

## What binds: the current policy

Decoding the firmware's policy status (RM control 0x2080A619) during load showed the arbitrated clock equal to the offered clock of policy 12, a current-class policy, in the first clock domain in all [19 loaded samples](history-evidence/h25-loaded-policy.csv) (the second domain tied policies 0 and 12), with P13 reading 97–99 A against its 99 A limit, while board power still had about 7 W of headroom. P13 itself exports no clock, so the status alone does not single it out; the causal test below is what shows P13 was binding in these runs.

**Causal test (2026-10-24, panel, envelope boot, 600 s run, 21 verified writes):**

| Setting | SM clock vs adjacent 99 A references | P13 reading | P2 reading | Binding |
|---|---|---|---|---|
| 95 A, ×4 | −7.4 … −8.9 % (power −4 … −6 W) | 93.5–94.3 A | 113.5–114.9 W | current |
| 99 → 99 (null write) | within drift | 98.4 A | 118.7 W | current |
| 104 A, ×2 | +14.3 … +16.8 % | 103.0 A | 123.2–123.7 W | mixed |
| 109 A, ×2 | +29.0 … +29.7 % | 104.9–105.6 A | 125.3–125.9 W | board power |
| 114 A, ×2 | +16.3 … +26.8 % | 105.1–105.9 A | 125.5–126.1 W | board power |

- GPU temperature stayed well below the 85 °C guard (peak 64 °C in the unpublished 1 Hz telemetry; the published summaries carry no temperature column); the unpublished telemetry records only the software power cap flag and no Xid.
- A 104 A setting persisted for 60 s across the power daemon's periodic updates.
- Board power takes over at a P13 *reading* of about 105–106.5 A with the 125 W target. Above that, a higher P13 limit is not reached.
- An earlier identical run on an external monitor (`results/ladder-external/`) agrees on the mechanism but is not comparable with the panel history.

## FurMark score: 107 A vs 99 A, ABBA (2026-10-24, panel)

120 s benchmarks with at least 90 s cooldown between runs.

| Run | P13 | FurMark score | FPS min / avg / max | SM median | GPU W | max °C |
|---|---|---|---|---|---|---|
| A1 | 99 A | 10 658 | 34 / 89 / 93 | 772 | 117.3 | 58 |
| B1 | 107 A | 11 415 | 86 / 95 / 99 | 1005 | 124.6 | 60 |
| B2 | 107 A | 11 378 | 87 / 95 / 99 | 990 | 124.7 | 60 |
| A2 | 99 A | 10 728 | 86 / 89 / 93 | 765 | 117.8 | 59 |

- **Effect: +6.58 %** in score (mean B 11 396.5 vs mean A 10 693).
- The private experiment plan records a CLEAR threshold of max(3 × |A1 − A2| / mean(A), 1 %); with these scores, that is 1.96 %. The timing of the plan is not independently attested. The B runs differ by 0.32 %, the A runs by 0.65 %, and A2 ≥ A1 (the later reference score did not decline).
- SM +29.8 % for +6.0 % power: frames follow clock with an elasticity of about 0.22. FurMark on this GPU is partly bound elsewhere (memory stays at 5001 MHz, its P3 clock, throughout).

## Transport smoke test

Before any value changed, one identity write (99 000 → 99 000) through the privileged control returned status 0 and the full 6356-byte control block read back byte-identical before and after the write (`results/smoke/`).

## Not measured

- Games. No title has been measured with these limits.
- Board power above 125 W, although the envelope image allows 140 W.
- Regulator temperature.
- Any driver other than 595.99.02.

## About the per-run files

The seven `summary.txt` and `result.json` files are verbatim historical outputs.
Identical `identity.json` files are stored once, also verbatim:

| Runs | Identity |
|---|---|
| [Panel ladder](results/ladder-panel/), [external ladder](results/ladder-external/), [smoke](results/smoke/) | [ladder.json](results/identities/ladder.json) |
| [A1](results/abba-A1-99000/), [A2](results/abba-A2-99000/) | [hold-99.json](results/identities/hold-99.json) |
| [B1](results/abba-B1-107000/), [B2](results/abba-B2-107000/) | [hold-107.json](results/identities/hold-107.json) |

The smoke identity contains the ladder schedule by an old reporting bug; smoke did
not execute it. `S1-A`/`S1-B` and `design` strings are historical experiment labels.
Per-boot UUIDs are retained to identify the shared boot. These runs predate the
portability and release cleanup; the current tools have CPU validation only.
