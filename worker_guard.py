"""Run an untrusted worker with a memory budget for its whole process tree.

macOS does not enforce RLIMIT_AS, RLIMIT_DATA or RLIMIT_RSS: setrlimit refuses
them and allocations past them succeed (measured 2026-09-23, macOS 26.5.2). So
the parent watches instead. The worker starts in its own process group; every
POLL seconds the parent sums the physical footprint (what macOS counts as the
process's memory, compressed pages included) of the group, of every descendant
it can find, and of every process it has seen before. Past the limit, all of
them are killed with SIGKILL, reaped, and the caller is told "memory_limit".

Tolerance: the tree can grow between two polls. At POLL = 5 ms the overshoot
measured in memory_guard tests stays under TOLERANCE; tests assert it.

    run(argv, limit, timeout, env, cwd, preexec) -> Result
"""
import ctypes
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

POLL = 0.005
TOLERANCE = 256 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
RUSAGE_INFO_V2 = 2


class _Rusage(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins",
        "wired_size", "resident_size", "phys_footprint", "proc_start_abstime",
        "proc_exit_abstime", "child_user_time", "child_system_time", "child_pkg_idle_wkups",
        "child_interrupt_wkups", "child_pageins", "child_elapsed_abstime", "diskio_bytesread",
        "diskio_byteswritten")]


def _libproc():
    try:
        return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None


LIBPROC = _libproc()


def available():
    return LIBPROC is not None


def _pids(fn, key):
    buf = (ctypes.c_int * 4096)()
    n = fn(key, buf, ctypes.sizeof(buf))
    return [p for p in buf[:max(n, 0)] if p > 0]


def footprint(pid):
    """Physical footprint in bytes, or 0 when the process is gone."""
    ru = _Rusage()
    if LIBPROC.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(ru)) != 0:
        return 0
    return ru.phys_footprint


def tree(root, seen):
    """Every live process of root's group, its descendants, and anything seen before."""
    todo = [root] + _pids(LIBPROC.proc_listpgrppids, root) + list(seen)
    found = set()
    while todo:
        pid = todo.pop()
        if pid in found:
            continue
        found.add(pid)
        todo.extend(_pids(LIBPROC.proc_listchildpids, pid))
    return found


@dataclass
class Result:
    outcome: str            # "ok", "memory_limit", "timeout", "too_much_output", "crashed"
    returncode: int
    stdout: str
    peak: int               # largest tree footprint the parent measured, bytes
    maxrss: int             # the worker's own peak RSS from the kernel, bytes
    processes: int          # distinct processes seen in the tree


def _kill(pids, group):
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def run(argv, limit, timeout, env=None, cwd=None, preexec=None):
    if not available():
        raise RuntimeError("no way to measure the worker's memory")
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, env=env, cwd=cwd, preexec_fn=preexec,
                            start_new_session=True)
    out = []

    def read():
        # never hold more than MAX_OUTPUT of what the worker says
        size = 0
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            size += len(chunk)
            if size > MAX_OUTPUT:
                out.append(None)
                return
            out.append(chunk)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    pid = 0
    seen, live, peak, outcome, status, ru = set(), set(), 0, None, 0, None
    deadline = time.monotonic() + timeout
    try:
        while True:
            pid, status, ru = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                break
            sizes = {p: footprint(p) for p in tree(proc.pid, seen)}
            live = {p for p, size in sizes.items() if size}
            seen |= live
            used = sum(sizes.values())
            peak = max(peak, used)
            if used > limit:
                outcome = "memory_limit"
            elif None in out:
                outcome = "too_much_output"
            elif time.monotonic() > deadline:
                outcome = "timeout"
            if outcome:
                break
            time.sleep(POLL)
    finally:
        # the worker and whatever it started die together and are reaped; only
        # processes alive at the last look are signalled, never a recycled pid
        _kill(live, proc.pid)
        if not pid:
            _pid, status, ru = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)
        reader.join(5)
        proc.stdout.close()
    if outcome is None:
        outcome = "ok" if proc.returncode == 0 else "crashed"
    if None in out:
        outcome = "too_much_output"
    text = b"".join(c for c in out if c).decode("utf-8", "replace") if outcome == "ok" else ""
    return Result(outcome, proc.returncode, text,
                  peak, ru.ru_maxrss if ru else 0, len(seen))
