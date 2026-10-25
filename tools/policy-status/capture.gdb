# Root-run: one fixed A618 or A619 GET with a separate guarded buffer. No Set/retry.
set pagination off
set confirm off
set breakpoint pending on
set auto-load off
python
import gdb
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
import time
from pathlib import Path

sys.path.insert(0, os.environ['PT516_POLICY_LAB'])
from codec import CANARY, EXPECTED, INFO_SIZE, inventory, request, status as decode_status

out = Path(os.environ['PT516_POLICY_DIR'])
query = os.environ['PT516_POLICY_QUERY']
info = (out.parent/'info/a618.bin').read_bytes() if query == 'status' else None
COMMAND, REQUEST = request(query, info)
SIZE = len(REQUEST)
name = 'a618' if query == 'info' else 'a619'
state = {'armed': False, 'issued': False, 'dispatched': False, 'complete': False}

def reg(name):
    return int(gdb.parse_and_eval('$' + name))

def stop(reason):
    gdb.write('POLICY STOP: %s\n' % reason)
    return True

def read(pointer, size):
    return bytes(gdb.selected_inferior().read_memory(pointer, size))

def guards(pointer):
    return (read(pointer - len(CANARY), len(CANARY)) == CANARY and
            read(pointer + SIZE, len(CANARY)) == CANARY)

class NoSetter(gdb.Breakpoint):
    def stop(self):
        return stop('public setter reached during GET inspection')

class Received(gdb.Breakpoint):
    def stop(self):
        try:
            if not state['dispatched'] or state['complete']:
                raise ValueError('unexpected or repeated callback result')
            status = reg('eax') & 0xffffffff
            raw = read(state['pointer'], SIZE)
            guard_ok = guards(state['pointer'])
            source_ok = read(state['source'], 0x21b8) == state['source_bytes']
            result = {'utc': datetime.now(timezone.utc).isoformat(), 'pci': '0000:01:00.0',
                      'command': hex(COMMAND), 'size': SIZE,
                      'host_epoch_start': state['start_epoch'], 'host_epoch_end': time.time(),
                      'host_monotonic_start': state['start_monotonic'], 'host_monotonic_end': time.monotonic(), 'rm_callback_status': '0x%x' % status,
                      'callback_count': 1, 'library_sha256': EXPECTED,
                      'request': 'separate owned buffer; zeroed except validated A619 policy mask',
                      'request_sha256': hashlib.sha256(REQUEST).hexdigest(),
                      'reply_sha256': hashlib.sha256(raw).hexdigest(),
                      'buffer_guards_intact': guard_ok, 'original_a618_buffer_unchanged': source_ok}
            # Preserve even failed/partial replies before any interpretation or retry.
            (out / (name + '.bin')).open('xb').write(raw)
            (out / (name + '.json')).open('x').write(json.dumps(result, indent=2) + '\n')
            gdb.write('POLICY RESULT\n' + json.dumps(result, indent=2) + '\n')
            if not guard_ok or not source_ok:
                raise ValueError('buffer guard or original A618 buffer changed; raw reply preserved')
            if status == 0:
                decoded = inventory(raw) if query == 'info' else decode_status(raw, info)
                (out / 'decoded.json').open('x').write(json.dumps(decoded, indent=2) + '\n')
                gdb.write('POLICY decoded %d records; full data saved\n' % len(decoded['policies']))
            else:
                raise ValueError('nonzero RM status; reply left uninterpreted')
            state['complete'] = True
            gdb.write('POLICY capture complete; stopped before retry or NVML reply interpretation\n')
        except Exception as exc:
            return stop(str(exc))
        return True

class Dispatch(gdb.Breakpoint):
    def stop(self):
        try:
            if state['dispatched'] or not state['issued']:
                raise ValueError('unexpected or repeated RM callback')
            if reg('edx') != COMMAND or reg('r8d') != SIZE or reg('rcx') != state['pointer']:
                raise ValueError('RM callback command/buffer/size differs')
            state['dispatched'] = True
            return False
        except Exception as exc:
            return stop(str(exc))

class Wrapper(gdb.Breakpoint):
    def __init__(self, base):
        super().__init__('*0x%x' % (base + 0x9c780), internal=True)
        self.base = base

    def stop(self):
        try:
            command = reg('ecx') & 0xffffffff
            if (command & 0xffffc000) == 0x2080c000:
                raise ValueError('privileged legacy control reached')
            if command != 0x2080a618:
                return False
            if state['issued']:
                raise ValueError('second A618 interception')
            caller = int.from_bytes(read(reg('rsp'), 8), 'little')
            source = reg('r8')
            if caller != self.base + 0xf3213 or reg('r9d') != 0x21b8 or source == 0:
                raise ValueError('unexpected A618 caller, buffer or size')
            descriptor = json.loads((out / 'buffer.json').read_text())
            if descriptor['pid'] != gdb.selected_inferior().pid or descriptor['size'] != SIZE:
                raise ValueError('buffer descriptor PID/size differs')
            pointer = descriptor['pointer']
            if type(pointer) is not int or not 0 < pointer < (1 << 47):
                raise ValueError('invalid buffer pointer')
            if pointer - len(CANARY) < source + 0x21b8 and source < pointer + SIZE + len(CANARY):
                raise ValueError('replacement buffer overlaps A618 allocation')
            if not guards(pointer) or read(pointer, SIZE) != REQUEST:
                raise ValueError('replacement buffer not pristine or guard differs')
            state.update(issued=True, pointer=pointer, source=source, source_bytes=read(source, 0x21b8),
                         start_epoch=time.time(), start_monotonic=time.monotonic())
            gdb.execute('set $ecx = 0x%x' % COMMAND)
            gdb.execute('set $r8 = 0x%x' % pointer)
            gdb.execute('set $r9d = 0x%x' % SIZE)
            Received('*0x%x' % (self.base + 0x9c7fd), internal=True)
            Dispatch('*0x%x' % (self.base + 0x9c7fa), internal=True)
            gdb.write('POLICY one %s GET armed; separate %d-byte buffer\n' % (name.upper(), SIZE))
            return False
        except Exception as exc:
            return stop(str(exc))

class Bounds(gdb.Breakpoint):
    def stop(self):
        try:
            if state['armed']:
                raise ValueError('second bounds getter')
            address = int(gdb.parse_and_eval('(void *) nvmlDeviceGetPowerManagementLimitConstraints'))
            library = gdb.solib_name(address)
            with open(library, 'rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != EXPECTED:
                    raise ValueError('loaded NVML hash differs')
            Wrapper(address - 0x7c470)
            state['armed'] = True
            return False
        except Exception as exc:
            return stop(str(exc))

NoSetter('nvmlDeviceSetPowerManagementLimit', internal=True)
NoSetter('nvmlDeviceSetPowerManagementLimit_v2', internal=True)
Bounds('nvmlDeviceGetPowerManagementLimitConstraints', internal=True)
end
run
python
if not state['complete']:
    gdb.write('POLICY STOP: no complete result\n')
end
quit
