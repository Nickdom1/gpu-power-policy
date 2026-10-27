"""Write runtime configuration and module parameters after the payload is built."""
import json
from pathlib import Path
import sys


def main(runtime_path, metadata_path, firmware_name, output):
    runtime = json.loads(Path(runtime_path).read_text())
    metadata = json.loads(Path(metadata_path).read_text())
    runtime.update({key: metadata[key] for key in ("output_size", "output_sha256")})
    destination = Path(output)
    (destination / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n")
    bdf, size = runtime["device"], runtime["output_size"]
    (destination / "modprobe.conf").write_text(
        f'options nvidia NVreg_RegistryDwordsPerDevice="pci={bdf};RmLinuxVbiosOverride=2;'
        f'RmLinuxVbiosOverrideSize={size}" NVreg_RegistryBinaryFilePerDevice="pci={bdf};'
        f'RmLinuxVbiosOverrideImage={firmware_name}"\n'
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
