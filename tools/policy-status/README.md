# Policy-status inspection

Read-only capture of the firmware inventory (RM `0x2080A618`) and status
(`0x2080A619`). The decoder reports numerical matches between offered and
arbitrated clocks; equality alone does not establish a unique binding limiter.

```sh
sudo bash run.sh --idle-only    # one inventory/status pair, no workload or write
python3 run.py --plan           # no hardware access
```

`codec.py`, `capture.gdb` and `bootstrap.py` implement the pinned GET transport and
decoder. `run.py` checks identity and the mode-dependent seed (99 A in mode 4).
`series.py`, `witness.py` and `xclient_pid.py` supply the panel-placement checks
used by the [current-limit experiments](../current-limit/).
