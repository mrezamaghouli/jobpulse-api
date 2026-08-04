"""
In-container deadline runner.

Why this exists: `scripts/run_collection_cycle_safe.sh` bounds each
`docker compose exec` call with a HOST-side `timeout`. That protects
against the host-side `docker` CLI process hanging, but it does NOT, by
itself, prove that the actual Python process running INSIDE the `api`
container stops when the host-side client disconnects -- `docker compose
exec`'s own behavior on a killed/disconnected client is not something
this repository controls or can fully rely on as the only bound. This
module is the SECOND, independent deadline: it runs entirely inside the
container, wrapping the actual collector module invocation in its own
process-group-scoped timeout, so the in-container process stops on its
own even if the host-side connection is severed uncleanly.

Usage (inside the `api` container):

    python -m scripts.run_with_deadline --seconds 1800 --kill-after 10 \
        -- python -m scripts.linkedin_auth_preflight

Design:
  - The target command is started in a NEW process group
    (`start_new_session=True`, which calls `setsid()` on POSIX) so every
    process it forks lives in that same group and can be signaled
    together -- mirroring the host-side `timeout` (no `--foreground`)
    behavior this module is the in-container counterpart to.
  - On deadline: SIGTERM the whole process group, wait up to
    `--kill-after` seconds for a clean exit, then SIGKILL the whole group
    if it's still alive.
  - This runner ALSO forwards its own SIGTERM/SIGINT/SIGHUP to that same
    process-group cleanup: if something external kills the runner itself
    (e.g. `docker compose exec`'s client disconnecting, an operator
    sending Ctrl-C, a session hangup), the target's process group is
    still terminated before the runner exits -- it does not merely die
    and abandon a separately-sessioned child (which `start_new_session`
    would otherwise orphan, immune to signals sent to the runner's own
    process group).
  - Exit codes:
      TIMEOUT_EXIT_CODE (124, matching GNU coreutils `timeout`'s own
        convention) when the `--seconds` deadline itself fires.
      128 + signum (the standard shell convention for "terminated by
        signal N") when the runner is stopped by an external
        SIGTERM/SIGINT/SIGHUP.
      INTERNAL_ERROR_EXIT_CODE (125) for any other unexpected failure
        after the child was started, always preceded by the same
        process-group cleanup.
      Otherwise, the child's own exit code is propagated unchanged.
"""
import argparse
import math
import os
import signal
import subprocess
import sys
import time


TIMEOUT_EXIT_CODE = 124
SIGNAL_EXIT_BASE = 128
INTERNAL_ERROR_EXIT_CODE = 125

MAX_SECONDS = 86400.0     # 24h -- generous but not unbounded for a single deadline
MAX_KILL_AFTER = 300.0    # 5 minutes -- a grace period should never itself be huge

_TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

# Set by _handle_termination_signal() -- read by the polling loop in
# run_with_deadline(). Deliberately the ONLY thing the signal handler
# does: no I/O, no process manipulation, no exceptions raised from
# within the handler itself. All real cleanup work happens in normal
# control flow once the main loop notices this flag, per the standard
# "signal handlers should do as little as possible" guidance -- this
# also means SIGTERM/SIGINT/SIGHUP are all safe to receive at any point,
# including while another signal is already being processed.
_received_signal = None


def _handle_termination_signal(signum, frame):
    global _received_signal
    if _received_signal is None:
        _received_signal = signum


def _terminate_process_group(process: subprocess.Popen, kill_after: float) -> None:
    """Central, single cleanup path used by every caller (deadline
    expiry, an external signal, or an unexpected exception): SIGTERM the
    whole process group, wait up to `kill_after` seconds, then SIGKILL if
    still alive. Tolerant of the child having ALREADY exited at any
    point (a real, expected race -- not a bug) -- never raises
    ProcessLookupError or any other exception to the caller."""
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return  # already gone -- nothing to clean up

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return  # exited between getpgid() and killpg() -- also fine

    grace_deadline = time.monotonic() + kill_after
    while time.monotonic() < grace_deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # exited during the grace window -- also fine

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # Group was SIGKILLed; if the direct child still hasn't been
        # reaped within 5s something unusual is happening (e.g. it's
        # itself stuck in uninterruptible I/O), but we must not hang
        # this cleanup function forever -- the caller still exits.
        pass


def run_with_deadline(cmd: list[str], seconds: float, kill_after: float) -> int:
    global _received_signal
    _received_signal = None

    previous_handlers = {}
    for sig in _TERMINATION_SIGNALS:
        previous_handlers[sig] = signal.signal(sig, _handle_termination_signal)

    process = None
    try:
        process = subprocess.Popen(cmd, start_new_session=True)
        deadline = time.monotonic() + seconds

        while True:
            if _received_signal is not None:
                _terminate_process_group(process, kill_after)
                return SIGNAL_EXIT_BASE + _received_signal

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                return process.wait(timeout=min(remaining, 0.5))
            except subprocess.TimeoutExpired:
                continue

        # Deadline hit (not a signal) -- same central cleanup path.
        _terminate_process_group(process, kill_after)
        return TIMEOUT_EXIT_CODE

    except BaseException as exc:
        # Any unexpected exception after Popen (including a signal that
        # happens to arrive as a raw KeyboardInterrupt in the brief
        # window before our own handler is installed) must still clean
        # up the process group before this function returns -- an
        # internal bug in the runner itself must never be the reason a
        # child or grandchild is left running.
        if process is not None:
            _terminate_process_group(process, kill_after)
        print(f"run_with_deadline: unexpected failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return INTERNAL_ERROR_EXIT_CODE

    finally:
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def _finite_float(raw: str, *, minimum: float, inclusive_minimum: bool, maximum: float) -> float:
    """Strict numeric parser for argparse `type=`. Rejects NaN, Infinity,
    -Infinity (all of which Python's own `float()` parses successfully
    from the literal strings "nan"/"inf"/"-inf"/"Infinity" without
    raising -- `math.isfinite()` is what actually catches them), any
    malformed string, and anything outside the documented bounds.
    argparse itself turns an ArgumentTypeError into a clean usage error
    and a non-zero exit -- no uncontrolled traceback."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{raw!r} is not a valid number")

    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{raw!r} must be finite (NaN/Infinity are rejected)")

    if inclusive_minimum:
        if value < minimum:
            raise argparse.ArgumentTypeError(f"{raw!r} must be >= {minimum}")
    else:
        if value <= minimum:
            raise argparse.ArgumentTypeError(f"{raw!r} must be > {minimum}")

    if value > maximum:
        raise argparse.ArgumentTypeError(f"{raw!r} must be <= {maximum}")

    return value


def _seconds_type(raw: str) -> float:
    return _finite_float(raw, minimum=0, inclusive_minimum=False, maximum=MAX_SECONDS)


def _kill_after_type(raw: str) -> float:
    return _finite_float(raw, minimum=0, inclusive_minimum=True, maximum=MAX_KILL_AFTER)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a command under an in-process-group deadline.")
    parser.add_argument("--seconds", type=_seconds_type, required=True,
                         help=f"deadline in seconds; finite, > 0, <= {MAX_SECONDS}")
    parser.add_argument("--kill-after", type=_kill_after_type, required=True,
                         help=f"SIGKILL grace period in seconds; finite, >= 0, <= {MAX_KILL_AFTER}")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]

    if not cmd:
        print("run_with_deadline: no command given after --", file=sys.stderr)
        return 2

    return run_with_deadline(cmd, args.seconds, args.kill_after)


if __name__ == "__main__":
    sys.exit(main())
