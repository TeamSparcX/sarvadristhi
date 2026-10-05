"""Read-only evidence source over an image file or block device."""
import mmap
import os
import stat
import sys


# Windows: expose the buffer operations parsers need without mapping a physical drive.
class ChunkedReader:
    """Small read-only bytes-like interface for Windows physical drives."""
    def __init__(self, fd: int, size: int, sector: int = 512):
        self.fd, self.size, self.sector = fd, size, sector

    def __len__(self):
        return self.size

    def _read(self, start: int, end: int) -> bytes:
        start, end = max(0, start), min(self.size, end)
        if end <= start:
            return b""
        aligned_start = start // self.sector * self.sector
        aligned_end = ((end + self.sector - 1) // self.sector) * self.sector
        os.lseek(self.fd, aligned_start, os.SEEK_SET)
        data = os.read(self.fd, aligned_end - aligned_start)
        return data[start - aligned_start:min(len(data), end - aligned_start)]

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self.size)
            data = self._read(start, stop)
            return data[::step]
        i = key + self.size if key < 0 else key
        if i < 0 or i >= self.size:
            raise IndexError("evidence offset out of range")
        return self._read(i, i + 1)[0]

    def find(self, sub: bytes, start: int = 0, end: int | None = None) -> int:
        end = self.size if end is None else min(end, self.size)
        start = max(0, start)
        if not sub:
            return min(start, end)
        # Bounded chunks keep physical-drive scans from loading the entire device.
        chunk_size = 8 * 1024 * 1024
        pos = start
        while pos < end:
            stop = min(end, pos + chunk_size)
            data = self._read(pos, min(end, stop + len(sub) - 1))
            found = data.find(sub)
            if found >= 0:
                return pos + found
            pos = stop
        return -1


# Windows: \\.\PhysicalDriveN and \\.\X: are device paths; they cannot be memory-mapped.
def _windows_device(path) -> bool:
    return sys.platform == "win32" and str(path).startswith("\\\\.\\")


def require_image(path) -> None:
    """Windows: parsers need a mapped image, so physical drives must be acquired first."""
    if _windows_device(path):
        raise RuntimeError(f"{path} is a physical drive. On Windows, image it first "
                           f"(sarvadristhi acquire {path} <image> --case <dir>, or analyze --acquire-to "
                           f"<image>) and analyze the image.")


class Source:
    def __init__(self, path: str):
        self.path = path
        # Windows: binary mode prevents newline translation and detects raw physical drives.
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        self.is_device = _windows_device(path)
        try:
            self.fd = os.open(path, flags)
        except OSError as e:
            if self.is_device:
                raise PermissionError(f"Cannot open {path}; run an Administrator terminal and "
                                      "attach the drive through a hardware write-blocker.") from e
            raise
        st = os.fstat(self.fd)
        self.is_device = self.is_device or stat.S_ISBLK(st.st_mode)
        if self.is_device and sys.platform == "win32":
            # Windows: physical drives do not support mmap or seek-to-end for size.
            import ctypes
            import msvcrt
            from ctypes import wintypes
            class DISK_LENGTH(ctypes.Structure):
                _fields_ = [("Length", ctypes.c_longlong)]
            result = DISK_LENGTH()
            returned = wintypes.DWORD()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # Windows: so get_last_error() is real
            ok = kernel32.DeviceIoControl(
                wintypes.HANDLE(msvcrt.get_osfhandle(self.fd)), 0x0007405C, None, 0,
                ctypes.byref(result), ctypes.sizeof(result), ctypes.byref(returned), None)
            if not ok:
                os.close(self.fd)
                raise OSError(ctypes.get_last_error(), "Could not query physical drive size")
            self.size = result.Length
            self.buf = ChunkedReader(self.fd, self.size)
        else:
            self.size = os.lseek(self.fd, 0, os.SEEK_END)
            self.buf = mmap.mmap(self.fd, self.size, access=mmap.ACCESS_READ) if self.size else b""

    def read(self, offset: int, n: int) -> bytes:
        return self.buf[offset:offset + n]

    def device_readonly(self) -> bool | None:
        """Kernel read-only flag for block devices (advisory only; use a hardware blocker)."""
        # Windows: Linux sysfs has no equivalent status path, so report unknown.
        if not self.is_device or sys.platform == "win32":
            return None
        name = os.path.basename(os.path.realpath(self.path))
        try:
            with open(f"/sys/class/block/{name}/ro") as f:
                return f.read().strip() == "1"
        except OSError:
            return None

    def close(self):
        if isinstance(self.buf, mmap.mmap):
            self.buf.close()
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
