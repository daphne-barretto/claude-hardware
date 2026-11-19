"""Kernel-enforced limits for the build child: Landlock for files, seccomp for the rest.

These are the security boundary of the sandbox. Once applied they can't be lifted by
anything the process does later, so build code that gets past the Python-level guard
(the audit hook and import allowlist) still can't reach the network, start processes,
signal, trace or change the limits and scheduling of other processes, write or change
the mode, owner, timestamps or extended attributes (ACLs included) of files outside its
work directory, or read anything outside the Python installation and system libraries
(no repo checkout, no `.env`, no other process's `/proc` entry). Inside the work
directory it can create and write files but not delete or rename them or make
directories, and it can't make unnamed (O_TMPFILE) files, so everything it writes stays
visible to the parent's disk cap. It can't make in-memory files (memfd, System V shared
memory), which neither the disk cap nor RLIMIT_AS would count.

Both layers are applied by an unprivileged process, need no namespaces and work under
Docker's default seccomp profile. They apply to the calling process and every thread
and child it creates afterwards, so apply them while the process is single threaded.

Linux only (x86_64 and aarch64). Uses ctypes because Python has no wrappers for these
system calls.
"""

from __future__ import annotations

import ctypes
import os
import platform
import struct
import sys

# System call numbers. Landlock's are the same on every architecture.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

_ARCH = {
    # machine: (AUDIT_ARCH_*, SYS_seccomp, syscall numbers)
    "x86_64": (
        0xC000003E,
        317,
        {
            "open": 2,
            "shmget": 29,
            "shmat": 30,
            "socket": 41,
            "connect": 42,
            "accept": 43,
            "bind": 49,
            "listen": 50,
            "socketpair": 53,
            "clone": 56,
            "fork": 57,
            "vfork": 58,
            "execve": 59,
            "kill": 62,
            "truncate": 76,
            "chmod": 90,
            "fchmod": 91,
            "chown": 92,
            "fchown": 93,
            "lchown": 94,
            "ptrace": 101,
            "rt_sigqueueinfo": 129,
            "utime": 132,
            "setpriority": 141,
            "sched_setparam": 142,
            "sched_setscheduler": 144,
            "pivot_root": 155,
            "chroot": 161,
            "mount": 165,
            "umount2": 166,
            "init_module": 175,
            "delete_module": 176,
            "setxattr": 188,
            "lsetxattr": 189,
            "fsetxattr": 190,
            "removexattr": 197,
            "lremovexattr": 198,
            "fremovexattr": 199,
            "tkill": 200,
            "sched_setaffinity": 203,
            "tgkill": 234,
            "utimes": 235,
            "kexec_load": 246,
            "add_key": 248,
            "request_key": 249,
            "keyctl": 250,
            "ioprio_set": 251,
            "migrate_pages": 256,
            "openat": 257,
            "fchownat": 260,
            "futimesat": 261,
            "fchmodat": 268,
            "unshare": 272,
            "move_pages": 279,
            "utimensat": 280,
            "accept4": 288,
            "rt_tgsigqueueinfo": 297,
            "perf_event_open": 298,
            "prlimit64": 302,
            "name_to_handle_at": 303,
            "open_by_handle_at": 304,
            "setns": 308,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "finit_module": 313,
            "sched_setattr": 314,
            "memfd_create": 319,
            "bpf": 321,
            "execveat": 322,
            "userfaultfd": 323,
            "pidfd_send_signal": 424,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "pidfd_open": 434,
            "clone3": 435,
            "openat2": 437,
            "pidfd_getfd": 438,
            "memfd_secret": 447,
            "fchmodat2": 452,
            "setxattrat": 463,
            "removexattrat": 466,
        },
    ),
    "aarch64": (
        0xC00000B7,
        277,
        {
            "setxattr": 5,
            "lsetxattr": 6,
            "fsetxattr": 7,
            "removexattr": 14,
            "lremovexattr": 15,
            "fremovexattr": 16,
            "ioprio_set": 30,
            "umount2": 39,
            "mount": 40,
            "pivot_root": 41,
            "truncate": 45,
            "chroot": 51,
            "fchmod": 52,
            "fchmodat": 53,
            "fchownat": 54,
            "fchown": 55,
            "openat": 56,
            "utimensat": 88,
            "unshare": 97,
            "kexec_load": 104,
            "init_module": 105,
            "delete_module": 106,
            "ptrace": 117,
            "sched_setparam": 118,
            "sched_setscheduler": 119,
            "sched_setaffinity": 122,
            "kill": 129,
            "tkill": 130,
            "tgkill": 131,
            "rt_sigqueueinfo": 138,
            "setpriority": 140,
            "shmget": 194,
            "shmat": 196,
            "socket": 198,
            "socketpair": 199,
            "bind": 200,
            "listen": 201,
            "accept": 202,
            "connect": 203,
            "add_key": 217,
            "request_key": 218,
            "keyctl": 219,
            "clone": 220,
            "execve": 221,
            "migrate_pages": 238,
            "move_pages": 239,
            "rt_tgsigqueueinfo": 240,
            "perf_event_open": 241,
            "accept4": 242,
            "prlimit64": 261,
            "name_to_handle_at": 264,
            "open_by_handle_at": 265,
            "setns": 268,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "finit_module": 273,
            "sched_setattr": 274,
            "memfd_create": 279,
            "bpf": 280,
            "execveat": 281,
            "userfaultfd": 282,
            "pidfd_send_signal": 424,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "pidfd_open": 434,
            "clone3": 435,
            "openat2": 437,
            "pidfd_getfd": 438,
            "memfd_secret": 447,
            "fchmodat2": 452,
            "setxattrat": 463,
            "removexattrat": 466,
        },
    ),
}
_ARCH["arm64"] = _ARCH["aarch64"]

# Refused with EPERM: network, new programs and processes, tracing, other processes'
# memory, namespaces and mounts, kernel modules and keyrings, and kernel interfaces a
# build has no use for.
_DENIED = (
    "socket",
    "socketpair",
    "connect",
    "accept",
    "accept4",
    "bind",
    "listen",
    "fork",
    "vfork",
    "execve",
    "execveat",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "tkill",
    "pidfd_open",
    "pidfd_send_signal",
    "pidfd_getfd",
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "name_to_handle_at",
    "open_by_handle_at",
    "init_module",
    "finit_module",
    "delete_module",
    "kexec_load",
    "add_key",
    "request_key",
    "keyctl",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    # File mode, owner and timestamps. Landlock doesn't cover these, so without this a
    # build could chmod or touch files outside its work directory. Builds don't need
    # them on their own files either. (aarch64 has only the *at and f* variants.)
    "chmod",
    "fchmod",
    "fchmodat",
    "fchmodat2",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "utime",
    "utimes",
    "futimesat",
    "utimensat",
    # Extended attributes, also outside Landlock. A POSIX ACL (`system.posix_acl_access`)
    # sets a file's mode, so these are the chmod hole again. setxattrat and removexattrat
    # are new in Linux 6.13; older kernels don't have them and refusing them costs nothing.
    "setxattr",
    "lsetxattr",
    "fsetxattr",
    "removexattr",
    "lremovexattr",
    "fremovexattr",
    "setxattrat",
    "removexattrat",
    # Scheduling and memory placement of other processes (same uid is enough), and of
    # every process of the user or group for setpriority and ioprio_set.
    "setpriority",
    "ioprio_set",
    "sched_setparam",
    "sched_setscheduler",
    "sched_setattr",
    "migrate_pages",
    "move_pages",
    # In-memory files. Their pages live in shared memory, not the build's address space,
    # so RLIMIT_AS doesn't count them and the disk cap can't see them; the number of
    # memfds is bounded only by the open file limit. System V segments are the same and
    # also outlive the build. memfd_secret is new in Linux 5.14 and often disabled.
    "memfd_create",
    "memfd_secret",
    "shmget",
    "shmat",
)
# Refused only on kernels whose Landlock can't handle it (truncate needs ABI 3).
_DENIED_BELOW_ABI = {"truncate": 3}
# Allowed only when the target (first argument) is this process: (name, 0 also allowed).
# For prlimit64 and sched_setaffinity, 0 means the calling process; for kill it would
# mean the process group, which is refused.
_SELF_ONLY = (
    ("kill", False),
    ("tgkill", False),
    ("rt_sigqueueinfo", False),
    ("rt_tgsigqueueinfo", False),
    ("prlimit64", True),
    ("sched_setaffinity", True),
)

_CLONE_THREAD = 0x00010000
_O_TMPFILE = 0o20000000  # __O_TMPFILE, the same on x86_64 and aarch64
_EPERM = 1
_ENOSYS = 38

# Landlock filesystem rights, by the ABI version that introduced them.
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_DIR = 1 << 7
_FS_ABI1_ALL = (1 << 13) - 1
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1

_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER = 1
_SECCOMP_FILTER_FLAG_TSYNC = 1


class KernelSandboxUnavailable(RuntimeError):
    """The host can't apply the kernel limits (old kernel, Landlock disabled, ...)."""


_libc_handle: ctypes.CDLL | None = None


def _libc() -> ctypes.CDLL:
    """The C library, loaded on first use.

    `prctl` exists only on Linux (macOS has no such symbol), so it is looked up only after
    `apply` has checked the host.
    """
    global _libc_handle
    if _libc_handle is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.prctl.restype = ctypes.c_int
        _libc_handle = libc
    return _libc_handle


def _syscall(number: int, *args: int | ctypes.c_void_p | None) -> int:
    converted = [a if isinstance(a, ctypes.c_void_p) else ctypes.c_long(a or 0) for a in args]
    result = _libc().syscall(ctypes.c_long(number), *converted)
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return result


def landlock_abi() -> int:
    """Landlock ABI version the kernel supports; 0 if it has none or it is disabled."""
    try:
        return _syscall(_SYS_LANDLOCK_CREATE_RULESET, None, 0, 1)  # VERSION flag
    except OSError:
        return 0


def _apply_landlock(abi: int, read_paths: list[str], write_paths: list[str]) -> None:
    handled_fs = _FS_ABI1_ALL
    if abi >= 2:
        handled_fs |= _FS_REFER
    if abi >= 3:
        handled_fs |= _FS_TRUNCATE
    if abi >= 5:
        handled_fs |= _FS_IOCTL_DEV
    # No net or scope rules are added, so handling them denies all TCP bind/connect and
    # signals to processes outside the sandbox, on kernels that support them.
    if abi >= 6:
        attr = struct.pack(
            "=QQQ",
            handled_fs,
            _NET_BIND_TCP | _NET_CONNECT_TCP,
            _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL,
        )
    elif abi >= 4:
        attr = struct.pack("=QQ", handled_fs, _NET_BIND_TCP | _NET_CONNECT_TCP)
    else:
        attr = struct.pack("=Q", handled_fs)
    attr_buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = _syscall(
        _SYS_LANDLOCK_CREATE_RULESET, ctypes.c_void_p(ctypes.addressof(attr_buf)), len(attr), 0
    )
    try:
        file_rights = _FS_EXECUTE | _FS_READ_FILE
        dir_rights = file_rights | _FS_READ_DIR
        rules = [(p, dir_rights, file_rights) for p in read_paths]
        write_file = file_rights | _FS_WRITE_FILE | (handled_fs & (_FS_TRUNCATE | _FS_IOCTL_DEV))
        # Everything in the write paths but removing, renaming and making directories: a
        # file deleted (or renamed away) while open still takes space, and the parent's
        # disk cap counts only what it sees in the work directory, which stays flat.
        write_dir = handled_fs & ~(_FS_REMOVE_DIR | _FS_REMOVE_FILE | _FS_MAKE_DIR)
        rules += [(p, write_dir, write_file) for p in write_paths]
        for path, on_dir, on_file in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # missing on this host
            try:
                rights = on_dir if os.path.isdir(path) else on_file
                rule = struct.pack("=Qi", rights & handled_fs, fd)
                rule_buf = ctypes.create_string_buffer(rule, len(rule))
                _syscall(
                    _SYS_LANDLOCK_ADD_RULE,
                    ruleset,
                    1,  # LANDLOCK_RULE_PATH_BENEATH
                    ctypes.c_void_p(ctypes.addressof(rule_buf)),
                    0,
                )
            finally:
                os.close(fd)
        _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)
    finally:
        os.close(ruleset)


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


_LD_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_JSET = 0x45  # BPF_JMP | BPF_JSET | BPF_K
_RET = 0x06  # BPF_RET | BPF_K
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_RET_ALLOW = 0x7FFF0000
_OFF_NR, _OFF_ARCH, _OFF_ARG0 = 0, 4, 16


def denied_syscalls(machine: str, abi: int) -> list[str]:
    """Names of the system calls the filter refuses outright on this machine."""
    numbers = _ARCH[machine][2]
    names = list(_DENIED) + [n for n, needs in _DENIED_BELOW_ABI.items() if abi < needs]
    return [n for n in names if n in numbers]


def seccomp_program(machine: str, pid: int, abi: int) -> list[tuple[int, int, int, int]]:
    """The seccomp filter as (code, jt, jf, k) BPF instructions."""
    arch, _, numbers = _ARCH[machine]
    prog = [
        (_LD_ABS, 0, 0, _OFF_ARCH),
        (_JEQ, 1, 0, arch),
        (_RET, 0, 0, _RET_KILL_PROCESS),
        (_LD_ABS, 0, 0, _OFF_NR),
    ]
    if machine == "x86_64":  # the x32 ABI would bypass the numbers below
        prog += [(_JGE, 0, 1, 0x40000000), (_RET, 0, 0, _RET_KILL_PROCESS)]
    for name in denied_syscalls(machine, abi):
        prog += [(_JEQ, 0, 1, numbers[name]), (_RET, 0, 0, _RET_ERRNO | _EPERM)]
    # clone3 hides its flags in memory a filter can't read; ENOSYS makes libc fall back
    # to clone, whose flags it can.
    prog += [(_JEQ, 0, 1, numbers["clone3"]), (_RET, 0, 0, _RET_ERRNO | _ENOSYS)]
    # No unnamed files (O_TMPFILE): Landlock doesn't stop them, and a file with no name in
    # the work directory takes disk space the parent's disk cap can't see. openat2 hides
    # its flags in memory too; ENOSYS, as for clone3 (libc's open doesn't use it).
    prog += [(_JEQ, 0, 1, numbers["openat2"]), (_RET, 0, 0, _RET_ERRNO | _ENOSYS)]
    for name, flags_arg in (("open", 1), ("openat", 2)):
        if name in numbers:  # aarch64 has only openat
            prog += [
                (_JEQ, 0, 4, numbers[name]),
                (_LD_ABS, 0, 0, _OFF_ARG0 + 8 * flags_arg),  # low 32 bits (little endian)
                (_JSET, 0, 1, _O_TMPFILE),
                (_RET, 0, 0, _RET_ERRNO | _EPERM),
                (_RET, 0, 0, _RET_ALLOW),
            ]
    # New threads only, no new processes.
    prog += [
        (_JEQ, 0, 4, numbers["clone"]),
        (_LD_ABS, 0, 0, _OFF_ARG0),
        (_JSET, 1, 0, _CLONE_THREAD),
        (_RET, 0, 0, _RET_ERRNO | _EPERM),
        (_RET, 0, 0, _RET_ALLOW),
    ]
    for name, zero_ok in _SELF_ONLY:
        if zero_ok:
            prog += [
                (_JEQ, 0, 5, numbers[name]),
                (_LD_ABS, 0, 0, _OFF_ARG0),
                (_JEQ, 2, 0, 0),
                (_JEQ, 1, 0, pid),
                (_RET, 0, 0, _RET_ERRNO | _EPERM),
                (_RET, 0, 0, _RET_ALLOW),
            ]
        else:
            prog += [
                (_JEQ, 0, 4, numbers[name]),
                (_LD_ABS, 0, 0, _OFF_ARG0),
                (_JEQ, 1, 0, pid),
                (_RET, 0, 0, _RET_ERRNO | _EPERM),
                (_RET, 0, 0, _RET_ALLOW),
            ]
    prog.append((_RET, 0, 0, _RET_ALLOW))
    return prog


def _apply_seccomp(machine: str, abi: int) -> None:
    _, sys_seccomp, _ = _ARCH[machine]
    prog = seccomp_program(machine, os.getpid(), abi)
    filters = (_SockFilter * len(prog))(*[_SockFilter(*insn) for insn in prog])
    fprog = _SockFprog(len(prog), filters)
    # TSYNC applies the filter to every thread of the process, not just this one.
    _syscall(
        sys_seccomp,
        _SECCOMP_SET_MODE_FILTER,
        _SECCOMP_FILTER_FLAG_TSYNC,
        ctypes.c_void_p(ctypes.addressof(fprog)),
    )


def apply(read_paths: list[str], write_paths: list[str]) -> None:
    """Apply both layers, or raise KernelSandboxUnavailable.

    After a failure the process may hold some of the limits but not all, so it must not
    be treated as sandboxed.
    """
    if not sys.platform.startswith("linux"):
        # Checked before any syscall: Linux syscall numbers mean other calls elsewhere.
        raise KernelSandboxUnavailable(f"Landlock and seccomp need Linux, not {sys.platform}")
    machine = platform.machine()
    if machine not in _ARCH:
        raise KernelSandboxUnavailable(f"no seccomp filter for the {machine} architecture")
    abi = landlock_abi()
    if abi < 1:
        raise KernelSandboxUnavailable("Landlock is not available on this kernel")
    if _libc().prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise KernelSandboxUnavailable("could not set no_new_privs")
    try:
        _apply_landlock(abi, read_paths, write_paths)
    except OSError as exc:
        raise KernelSandboxUnavailable(f"could not apply Landlock: {exc}") from exc
    try:
        _apply_seccomp(machine, abi)
    except OSError as exc:
        # Landlock is already on and can't be undone, so the caller must not continue.
        raise KernelSandboxUnavailable(f"could not apply seccomp: {exc}") from exc
