# Root-run: one 125 W E61E request, changing only role 1 to verified role 8.
# Requires the boot image to expose policy 2 maximum PT516_POLICY_MAX_MW
# (125000 for the default generation, 140000 for the envelope); the request
# itself is always 125000 mW.
# Changes only this request packet role; no runtime policy/cache patching.
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

EXPECTED = '5176dd3ee63d5be9be66514a057fbcffb393ff46a0289f96fe1b923112967181'
POLICY_MAX_MW = int(os.environ.get('PT516_POLICY_MAX_MW', '125000'))
if POLICY_MAX_MW not in (125000, 140000):
    raise SystemExit('ROLE8 STOP: PT516_POLICY_MAX_MW is not a reviewed image maximum')
state = {'entered': False, 'device': None, 'submitted': False, 'rm_status': None}

def stop_error(exc):
    gdb.write('ROLE8 STOP: %s\n' % exc)
    return True

def reg(name):
    return int(gdb.parse_and_eval('$' + name))

class UnexpectedV2(gdb.Breakpoint):
    def stop(self):
        return stop_error('unexpected v2 setter')

class Returned(gdb.FinishBreakpoint):
    def stop(self):
        if not state['submitted'] or state['rm_status'] is None:
            return stop_error('setter returned without the expected RM call and return')
        status = reg('eax') & 0xffffffff
        gdb.write('ROLE8 public setter returned NVML status %d\n' % status)
        gdb.write('ROLE8 trace complete; one 125000 mW role-8 request\n')
        return False

class Hal(gdb.Breakpoint):
    def stop(self):
        try:
            if state['device'] is not None:
                raise ValueError('unexpected second HAL entry')
            if (reg('edx'), reg('ecx'), reg('r8d')) != (0, 0, 125000):
                raise ValueError('unexpected HAL scope/client/value')
            state['device'] = reg('rsi')
        except Exception as exc:
            return stop_error(exc)
        return False

class Request(gdb.Breakpoint):
    def stop(self):
        try:
            if state['submitted'] or not state['device']:
                raise ValueError('missing device or repeated request')
            if reg('ecx') != 0x2080e61e or reg('r9d') != 12:
                raise ValueError('unexpected command or parameter size')
            inferior = gdb.selected_inferior()
            device = state['device']
            ready, _, status = struct.unpack('<III', bytes(inferior.read_memory(device + 0x2306c, 12)))
            raw = bytes(inferior.read_memory(device + 0x23078, 0x21b8))
            if ready == 0 or status != 0:
                raise ValueError('policy cache not ready or has an error')
            roles = list(raw[0x44:0x57])
            if (raw[0x28], raw[0x2a], roles[1], roles[8], roles[3]) != (255, 2, 255, 2, 2):
                raise ValueError('role map differs from the observed absent-1 / policy-2 fallback')
            mask = struct.unpack_from('<I', raw, 4)[0]
            offset = 2 * 0xe4
            limits = struct.unpack_from('<III', raw, 0x60 + offset)
            if not mask & (1 << 2) or (raw[0x5c + offset], raw[0x5e + offset]) != (0, 0):
                raise ValueError('policy 2 is absent or has a different type/unit')
            if limits != (1000, 80000, POLICY_MAX_MW):
                raise ValueError('policy 2 limits differ from 1000/80000/%d mW' % POLICY_MAX_MW)
            address = reg('r8')
            before = bytes(inferior.read_memory(address, 12))
            expected = struct.pack('<BBHII', 1, 0xfe, 0, 125000, 0)
            if before != expected:
                raise ValueError('request differs from role=1/client=FE/value=125000/flags=0')
            after = b'\x08' + before[1:]
            # The only data write: the role byte of this process's stack packet.
            inferior.write_memory(address, b'\x08')
            if bytes(inferior.read_memory(address, 12)) != after:
                raise ValueError('modified packet did not verify; stopped before RM call')
            state['submitted'] = True
            gdb.write('ROLE8 request ' + json.dumps({
                'command': '0x2080e61e', 'role_before': 1, 'role_after': 8,
                'policy_index': 2, 'client': '0xfe', 'requested_mW': 125000,
                'policy_min_rated_max_mW': limits, 'roles': roles,
                'packet_before_hex': before.hex(), 'packet_after_hex': after.hex(),
                'a618_sha256': hashlib.sha256(raw).hexdigest()}) + '\n')
        except Exception as exc:
            return stop_error(exc)
        return False

class RmReturned(gdb.Breakpoint):
    def stop(self):
        if not state['submitted'] or state['rm_status'] is not None:
            return stop_error('unexpected RM return')
        state['rm_status'] = reg('eax') & 0xffffffff
        gdb.write('ROLE8 RM wrapper returned status 0x%x\n' % state['rm_status'])
        return False

class Setter(gdb.Breakpoint):
    def stop(self):
        try:
            if state['entered']:
                raise ValueError('unexpected second public setter call')
            address = int(gdb.parse_and_eval('(void *) nvmlDeviceSetPowerManagementLimit'))
            library = gdb.solib_name(address)
            with open(library, 'rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != EXPECTED:
                raise ValueError('library SHA-256 differs from the 595.99.02 research target')
            if reg('esi') != 125000:
                raise ValueError('public request is not exactly 125000 mW')
            base = address - 0x7ca70
            Hal('*0x%x' % (base + 0xf5b00), internal=True)
            Request('*0x%x' % (base + 0xf5be7), internal=True)
            RmReturned('*0x%x' % (base + 0xf5bec), internal=True)
            Returned(gdb.newest_frame(), internal=True)
            state['entered'] = True
            gdb.write('ROLE8 library hash verified; one request armed\n')
        except Exception as exc:
            return stop_error(exc)
        return False

UnexpectedV2('nvmlDeviceSetPowerManagementLimit_v2', internal=True)
Setter('nvmlDeviceSetPowerManagementLimit', internal=True)
end
run
# A guard failure stops the process; batch mode ends without continuing it.
quit
