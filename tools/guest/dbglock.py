"""Cross-process lock for mt7921dbg's shared "target" file: every tool that writes target and then a command file
(cmd, raw, mem) must hold it for the pair, or a concurrent tool can redirect the command to the other card."""
import contextlib
import fcntl

LOCK = "/tmp/fwdbg.lock"


@contextlib.contextmanager
def held():
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield
