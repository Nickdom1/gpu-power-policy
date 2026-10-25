# Root-run: long-lived P13 client. Each public 125 W setter call made by client.py is
# replaced, at the RM control wrapper, by one pending operation (A618/A61A/A619 GET or
# the privileged E61B SET) into this process's own guarded buffer. Nothing else is
# changed; a privileged command that is not the armed E61B stops the process.
set pagination off
set confirm off
set breakpoint pending on
set auto-load off
python
import gdb
import hashlib
import json
import os
import struct
from pathlib import Path

OPS = Path(os.environ['PT516_P13_OPS'])
EXPECTED = os.environ['PT516_P13_LIBRARY_SHA256']
CAP_MA = int(os.environ['PT516_P13_CAP_MA'])
FLOOR_MA = 94000
ALLOWED = {0x2080a618: ('info', 0x21b8), 0x2080a61a: ('get', 0x18d4),
           0x2080a619: ('status', 0x2e740), 0x2080e61b: ('set', 0x18d4)}
CANARY = bytes.fromhex('5054353136502d3133') * 2
state = {'base': None, 'armed': None, 'consumed': set(), 'sets': 0}

def reg(name):
    return int(gdb.parse_and_eval('$' + name))

def read(pointer, size):
    return bytes(gdb.selected_inferior().read_memory(pointer, size))

def stop(reason):
    gdb.write('P13 STOP: %s\n' % reason)
    return True

def pending():
    try:
        n = int((OPS/'current-op').read_text())
    except (OSError, ValueError):
        return None
    if n in state['consumed']:
        return None
    return n, json.loads((OPS/('op-%04d.buffer.json' % n)).read_text())

class Returned(gdb.Breakpoint):
    """RM wrapper dispatch returned: eax is the RM status for the swapped request."""
    def __init__(self, base, n):
        super().__init__('*0x%x' % (base + 0x9c7fd), internal=True)
        self.n = n
    def stop(self):
        try:
            # Returning False keeps the inferior running, so a temporary breakpoint would
            # never be deleted; disable this one explicitly after its single use.
            self.enabled = False
            status = reg('eax') & 0xffffffff
            op = state['armed']
            if op is None or op['n'] != self.n:
                raise ValueError('return without an armed operation')
            if not (read(op['pointer'] - len(CANARY), len(CANARY)) == CANARY and
                    read(op['pointer'] + op['size'], len(CANARY)) == CANARY):
                raise ValueError('buffer guard changed during op %d' % self.n)
            Path(OPS/('op-%04d.rm.json' % self.n)).write_text(json.dumps(dict(n=self.n, status=status)))
            gdb.write('P13 op %d %s rm_status=0x%x\n' % (self.n, op['kind'], status))
            state['armed'] = None
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
            if command != 0x2080e61e:
                if (command & 0xffffc000) == 0x2080c000:
                    raise ValueError('unexpected privileged control 0x%x' % command)
                return False
            if reg('r9d') != 12:
                raise ValueError('setter request with an unexpected size')
            item = pending()
            if item is None:
                raise ValueError('public setter reached with no pending operation; refusing an unmodified 125 W request')
            n, op = item
            if op['pid'] != gdb.selected_inferior().pid:
                raise ValueError('operation descriptor belongs to another process')
            if op['command'] not in ALLOWED or ALLOWED[op['command']] != (op['kind'], op['size']):
                raise ValueError('operation command/kind/size not allowed')
            pointer, size = op['pointer'], op['size']
            if type(pointer) is not int or not 0 < pointer < (1 << 47):
                raise ValueError('invalid buffer pointer')
            request = read(pointer, size)
            if hashlib.sha256(request).hexdigest() != op['request_sha256']:
                raise ValueError('buffer content differs from the descriptor')
            if not (read(pointer - len(CANARY), len(CANARY)) == CANARY and read(pointer + size, len(CANARY)) == CANARY):
                raise ValueError('buffer guards not pristine')
            if op['kind'] == 'set':
                # Defense in depth: the SET may only address policy 13, and only inside the window.
                if struct.unpack_from('<I', request, 0x10)[0] != 0x2000 or request[0x96c] != 15:
                    raise ValueError('SET block does not address policy 13 alone')
                if request[:16] == bytes(16):
                    raise ValueError('SET block has a zero header')
                value = struct.unpack_from('<I', request, 0x970)[0]
                if not FLOOR_MA <= value <= CAP_MA:
                    raise ValueError('SET value %d outside [%d, %d]' % (value, FLOOR_MA, CAP_MA))
                state['sets'] += 1
            gdb.execute('set $ecx = 0x%x' % op['command'])
            gdb.execute('set $r8 = 0x%x' % pointer)
            gdb.execute('set $r9d = 0x%x' % size)
            state['armed'] = dict(op, n=n)
            state['consumed'].add(n)
            Returned(self.base, n)
            return False
        except Exception as exc:
            return stop(str(exc))

class Setter(gdb.Breakpoint):
    def stop(self):
        try:
            if state['base'] is None:
                address = int(gdb.parse_and_eval('(void *) nvmlDeviceSetPowerManagementLimit'))
                library = gdb.solib_name(address)
                with open(library, 'rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != EXPECTED:
                        raise ValueError('loaded library hash differs')
                state['base'] = address - 0x7ca70
                Wrapper(state['base'])
                gdb.write('P13 hook armed on %s\n' % library)
            if reg('esi') != 125000:
                raise ValueError('public request is not 125000 mW')
            return False
        except Exception as exc:
            return stop(str(exc))

class NoV2(gdb.Breakpoint):
    def stop(self):
        return stop('unexpected v2 setter')

NoV2('nvmlDeviceSetPowerManagementLimit_v2', internal=True)
Setter('nvmlDeviceSetPowerManagementLimit', internal=True)
end
run
quit
