import ctypes
import os
import threading


BPF_MAP_UPDATE_ELEM = 2
BPF_OBJ_GET = 7
BPF_ANY = 0
SYS_BPF_X86_64 = 321

SLUG_HINT_NONE = 0
SLUG_HINT_READ = 1
SLUG_HINT_WRITE = 2
SLUG_HINT_BALANCED = 3
SLUG_HINT_PIPELINE = 4

DEFAULT_HINT_MAP = "/sys/fs/bpf/schedcp/slug_task_hints"

_libc = ctypes.CDLL(None, use_errno=True)
_tls = threading.local()


def _syscall(number, *args):
    return _libc.syscall(ctypes.c_long(number), *args)


def _bpf(cmd, attr):
    return _syscall(SYS_BPF_X86_64, ctypes.c_int(cmd),
                    ctypes.byref(attr), ctypes.c_uint(len(attr)))


def _u64_into(attr, offset, value):
    ctypes.c_uint64.from_buffer(attr, offset).value = value


def _u32_into(attr, offset, value):
    ctypes.c_uint32.from_buffer(attr, offset).value = value


def _obj_get(path):
    path_buf = ctypes.create_string_buffer(path.encode())
    attr = ctypes.create_string_buffer(32)
    _u64_into(attr, 0, ctypes.addressof(path_buf))
    return _bpf(BPF_OBJ_GET, attr)


def _map_update_elem(fd, key, value):
    key_obj = ctypes.c_uint32(key)
    value_obj = ctypes.c_uint32(value)
    attr = ctypes.create_string_buffer(32)
    _u32_into(attr, 0, fd)
    _u64_into(attr, 8, ctypes.addressof(key_obj))
    _u64_into(attr, 16, ctypes.addressof(value_obj))
    _u64_into(attr, 24, BPF_ANY)
    return _bpf(BPF_MAP_UPDATE_ELEM, attr)


def _hint_fd():
    fd = getattr(_tls, "slug_hint_fd", -2)
    if fd == -2:
        fd = _obj_get(os.environ.get("SLUG_HINT_MAP", DEFAULT_HINT_MAP))
        _tls.slug_hint_fd = fd
    return fd


def slug_mark_bb(hint):
    if getattr(_tls, "slug_cached_hint", None) == hint:
        return 0

    fd = _hint_fd()
    if fd < 0:
        return fd

    rc = _map_update_elem(fd, threading.get_native_id(), hint)
    if rc == 0:
        _tls.slug_cached_hint = hint
    return rc


def slug_mark_none_bb():
    return slug_mark_bb(SLUG_HINT_NONE)


def slug_mark_read_bb():
    return slug_mark_bb(SLUG_HINT_READ)


def slug_mark_write_bb():
    return slug_mark_bb(SLUG_HINT_WRITE)


def slug_mark_balanced_bb():
    return slug_mark_bb(SLUG_HINT_BALANCED)


def slug_mark_pipeline_bb():
    return slug_mark_bb(SLUG_HINT_PIPELINE)
