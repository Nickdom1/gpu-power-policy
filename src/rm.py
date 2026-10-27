"""Minimal Linux RM transport for the public NVIDIA 615.71.09 ABI."""
import ctypes
import fcntl
import os
import struct

VERSION = "615.71.09"
NVOS21 = struct.Struct("<IIIIQII")
NVOS54 = struct.Struct("<IIIIQII")
CARD_SIZE = 72


def ioctl_number(command, size):
    if not 0 < size < 16384:
        raise ValueError("invalid ioctl payload size")
    return 0xC0004600 | (size << 16) | command


def cards(raw):
    if not raw or len(raw) % CARD_SIZE:
        raise ValueError("CARD_INFO size mismatch")
    result = []
    for off in range(0, len(raw), CARD_SIZE):
        if raw[off]:
            domain, bus, slot, function = struct.unpack_from("<IBBB", raw, off + 4)
            result.append({"bdf": f"{domain:04x}:{bus:02x}:{slot:02x}.{function}",
                           "gpu_id": struct.unpack_from("<I", raw, off + 16)[0],
                           "minor": struct.unpack_from("<I", raw, off + 56)[0]})
    return result


def pointer(raw):
    return ctypes.addressof((ctypes.c_char * len(raw)).from_buffer(raw)) if raw else 0


class RM:
    def __init__(self, bdf):
        self.root = 0
        self.gpu_fd = None
        self.fd = os.open("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
        try:
            check = bytearray(struct.pack("<II64s", 0, 0, VERSION.encode()))
            self.ioctl(210, check)
            if struct.unpack_from("<I", check, 4)[0] != 1:
                raise RuntimeError("RM rejected driver version " + VERSION)
            self.root = self.alloc(0, 0, 0x41, bytearray())
            raw = bytearray(CARD_SIZE * 32)
            self.ioctl(200, raw)
            matches = [c for c in cards(raw) if c["bdf"] == bdf.lower()]
            if len(matches) != 1:
                raise RuntimeError("CARD_INFO did not uniquely identify " + bdf)
            # deviceConstruct checks that this process holds the selected GPU node.
            self.gpu_fd = os.open(f"/dev/nvidia{matches[0]['minor']}", os.O_RDWR | os.O_CLOEXEC)
            gpu = matches[0]["gpu_id"]
            # Attach only the selected GPU; the remaining IDs are invalid sentinels.
            self.control(0x215, struct.pack("<33I", gpu, *([0xFFFFFFFF] * 32)), self.root)
            _, info = self.control(0x205, struct.pack("<8I", gpu, *([0] * 7)), self.root)
            device, subdevice = struct.unpack_from("<II", info, 8)
            params = bytearray(56)
            struct.pack_into("<I", params, 0, device)
            self.device = self.alloc(self.root, 1, 0x80, params)
            self.subdevice = self.alloc(self.device, 2, 0x2080, bytearray(struct.pack("<I", subdevice)))
        except BaseException as original:
            try:
                self.close()
            except Exception as cleanup:
                original.add_note(f"RM rollback cleanup failed: {cleanup}")
            raise

    def ioctl(self, command, raw):
        fcntl.ioctl(self.fd, ioctl_number(command, len(raw)), raw, True)

    def alloc(self, parent, handle, cls, params):
        raw = bytearray(NVOS21.pack(self.root, parent, handle, cls, pointer(params), len(params), 0))
        self.ioctl(0x2B, raw)
        values = NVOS21.unpack(raw)
        self.check(values[-1], f"allocate class 0x{cls:x}")
        return values[2]

    @staticmethod
    def check(status, operation):
        if status:
            raise RuntimeError(f"RM {operation}: status 0x{status:08x}")

    def control(self, command, buf, object_handle=None):
        params = bytearray(buf)
        obj = self.subdevice if object_handle is None else object_handle
        raw = bytearray(NVOS54.pack(self.root, obj, command, 0, pointer(params), len(params), 0))
        self.ioctl(0x2A, raw)
        values = NVOS54.unpack(raw)
        self.check(values[-1], f"control 0x{command:08x}")
        if values[-2] != len(params):
            raise RuntimeError("RM changed the reply size")
        return values[-1], bytes(params)

    def close(self):
        fd, gpu_fd, root = self.fd, self.gpu_fd, self.root
        self.fd, self.gpu_fd, self.root = None, None, 0
        if fd is None:
            return
        errors = []
        if root:
            try:
                raw = bytearray(struct.pack("<IIII", root, 0, root, 0))
                fcntl.ioctl(fd, ioctl_number(0x29, len(raw)), raw, True)
                self.check(struct.unpack_from("<I", raw, 12)[0], "free root")
            except (OSError, RuntimeError) as error:
                errors.append(error)
        for descriptor in (gpu_fd, fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as error:
                    errors.append(error)
        if errors:
            for error in errors[1:]:
                errors[0].add_note(f"Additional RM cleanup failure: {error}")
            raise errors[0]

    def __enter__(self):
        return self

    def __exit__(self, _type, original, _traceback):
        try:
            self.close()
        except Exception as cleanup:
            if original is None:
                raise
            original.add_note(f"RM cleanup failed: {cleanup}")
