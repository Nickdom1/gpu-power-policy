#!/usr/bin/env python3
"""Check a saved kernel journal before further GPU requests; no device access."""

import argparse
import re
from pathlib import Path

HASHES = {
    3: "0501a4c0f0e4c488b1b5d4be0e846330c6fa2de4e4dd610c6d9ee00c1b025d61",
    1: "e8d799ceabb14c64d8b9d1d9623cea3e46a69fed7f43fc95c1dcd02f2c164752",
    2: "2e05d68ee0793dfa57f47e09d95f23cdf959726d7fa936d423a585179fd8b4b3",
    # Envelope: policy 2 maximum 140000 mW and policy 13 maximum 120000 mA, checksum 0xff.
    4: "57dd693f62954871243e94c17025a0207d94be45090ad144ab0a76883e325c73",
}
FATAL = re.compile(
    r"RmInitAdapter failed|rm_init_adapter failed|Cannot initialize GSP firmware RM|"
    r"kgspSendInitRpcs failed|GspMsgQueueSendCommand failed|unexpected WPR2 already up|"
    r"PT516 boot policy preparation refused|PT516 boot policy was requested but not registered|"
    r"NVRM: Xid",
    re.IGNORECASE,
)


def check(text, mode):
    lines = text.splitlines()
    for line in lines:
        if re.search(r"PT516 boot policy|NVRM:|nvidia.*error", line, re.IGNORECASE):
            print(line)
    statuses = re.findall(
        r"PT516 boot policy host registry status: (0x[0-9a-fA-F]+)", text
    )
    if FATAL.search(text) or any(int(s, 16) for s in statuses):
        print(
            "STOP: this boot records a GPU initialization/transport error or Xid. No further GPU request follows."
        )
        return 1
    required = [
        "PT516 boot policy command queue: 1572864 bytes",
        f"PT516 boot policy PREPARED mode={mode} bytes=516608 sha256={HASHES[mode]}",
    ]
    if not statuses or any(token not in text for token in required):
        print(
            "STOP: expected v2 queue, prepared image, or successful host registration is absent from the saved log."
        )
        return 1
    print(
        "Host preparation checks passed. These log entries alone do not verify GSP image use or policy acceptance."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("mode", type=int, choices=(1, 2, 3, 4))
    args = parser.parse_args()
    raise SystemExit(check(args.log.read_text(errors="replace"), args.mode))
