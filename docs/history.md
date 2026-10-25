# History

## Research method

The GPU core regulator topology was reviewed from circulating board documentation
and component datasheets, with Claude assisting, alongside the firmware's policy
interfaces. That review shaped the experiment plan. None of the board
documentation is reproduced here.

The experiments tested the firmware policy directly: lowering the current limit
reduced clocks reversibly; raising it moved the binding constraint toward board
power. Separate ABBA runs measured a **6.6 % higher FurMark score at 107 A than at
99 A**, with the board-power target held at 125 W ([results](results.md)). None of
this establishes a safe sustained regulator limit.

## Experiment ledger

A ledger reconstructed from retained experiment records and local Git history, including failed and inconclusive steps. Dates are 2026, local time, as recorded in those sources. IDs are the investigation's own. The selected source records below begin on October 17; the October 16 directory label alone is not evidence of when its contents existed.

| ID | Date | Test | Outcome |
|---|---|---|---|
| H01–H04 | 10-16/17 | Stock Turbo baseline; override powerd's target to 120 W; ordinary NVML 120 W request; both | Stock 111.9 W. The target override reached the existing 115 W maximum (+3.6 % score) but could not exceed it. NVML refused |
| H05 | 10-17 | Read the PCI ROM via sysfs | EIO, zero bytes |
| H06 | 10-17 | Trace the NVML request | It reached the driver; RM returned 0x56. Not a library-side refusal |
| H07 | 10-17 | Read the policy inventory (0x2080A618) | Role 1 absent; roles 3/8 map to policy 2; 1/80/115 W |
| H08 | 10-17 | Request with role 8 at 120 W | RM 0x1F: above the maximum |
| H11–H12 | 10-17 | Use the driver's own boot-time VBIOS extraction; UEFI metadata | 600 576-byte image obtained without touching the ROM; policy values located offline |
| H13 | 10-18 | First in-RAM policy image | Image prepared; GSP bootstrap queue exhausted |
| H14 | 10-18 | Enlarged queue | **Booted with policy 2 maximum 120 W**; the only difference among 13 decoded policy records compared was policy 2 maximum, 115 → 120 W. This did not attest the complete live image |
| H15 | 10-18 | Role-8 request after H14 | Accepted; idle limit unchanged (acceptance ≠ enforcement) |
| H16–H18 | 10-18 | Games with and without powerd and with the target override | Dynamic Boost stayed in control; the target path works |
| H19–H21 | 10-18 | FurMark external vs panel | 114.6 W / 72 FPS external; **117.6 W / 89 FPS panel** |
| H22–H23 | 10-18 | 125 W boot; then with the runtime override | 125 W target active; plateau unchanged at 117.6 W |
| H24 | 10-18 | Initial internal policy status on the external display | Two loaded snapshots; numerical clock matches suggested a candidate policy but did not establish the binding limiter |
| H25 | 10-22 | Dense internal policy status on the laptop panel | The first exported clock domain matched policy 12 alone in 19/19 loaded samples; P13 approximately 97–99 A. The second domain tied policies 0 and 12 |
| H26–H28 | 10-22 | Reversible voltage-floor pulse | +25 mV live → −12 % clocks, +1 W: a current-limited regime |
| — | 10-23 | Envelope image (P2 max 140 W, P13 max 120 A) built and inspected | Dated working note; hardware acceptance was not yet recorded |
| — | 10-24 | Envelope boot accepted by GSP | Initial acceptance preceded the P13 seed readback; later smoke/runtime results are in [results.md](results.md) |
| — | 10-24 | Smoke test, P13 ladder (panel), ABBA 99 vs 107 A | **+6.6 % FurMark score** |

The first image handoff exhausted the GSP bootstrap queue before credit returns
were available; enlarging the queue allowed the next boot to complete. Later,
the unchanged load plateau at a 125 W target redirected the investigation from
board-power settings to the firmware's current policy.

## Selected development evidence

These selected records show the progression from a refused request to changed
policy readback and an inconclusive clock observation. The later causal intervention
and score comparison are already published in [results.md](results.md) and
[results/](results/).

| Record | Source snapshot recorded | Released artifact | Scope |
|---|---|---|---|
| H06 | October 17, `e9d351c` | [Refused-request report excerpt](history-evidence/h06-rm-refusal.report-excerpt.json) | An exact excerpt from a report of operator-provided output, not the original trace. The 120 W request reached RM and was refused; the enforced limit stayed at 80 W |
| H14 | October 18, `fcf7260` | [Selected readback fields](history-evidence/h14-readback.selected.json) | Preserves all 13 decoded policy records and the original comparison. The idle enforced limit remained 80 W; this was not a sustained-load result |
| H24 | October 18, `66aa86b` | [Clock comparison](history-evidence/h24-clock-check.json) | Unchanged offline analysis of the earlier H24 capture, including its numerical-equality caveat and the P0/P12 tie in one domain |

The [source manifest](history-evidence/source-manifest.json) identifies each
original commit, blob, file hash, recorded author/committer date and export method.
H14 omits attachment paths, boot identifiers, temporary directories and internal
instructions. Its retained values are unchanged. H24 is copied byte for byte.
The original development repository is private; its Git objects and omitted
material are not included here. The published excerpts can be inspected directly,
but the private-source hashes cannot be independently checked from this release
alone.

These local development records are not independently attested timestamps and do
not establish publication priority. Related public work is credited in
[prior-art.md](prior-art.md). The H25 panel series is separate from H24; its
[19-row decoded table](history-evidence/h25-loaded-policy.csv) is published with unchanged values.
