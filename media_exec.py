"""Run FFmpeg tools with bounded time and bounded captured output."""
import subprocess
import threading

MAX_STDERR = 16 * 1024


class OutputLimitError(ValueError):
    """A child process wrote more than the caller agreed to hold in memory."""


def run_bounded(cmd, *, timeout, max_output, check=False):
    """Like subprocess.run(capture_output=True) but never buffers more than
    ``max_output`` bytes of stdout (the child is killed instead) and keeps
    only the last MAX_STDERR bytes of stderr."""
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, errors = bytearray(), bytearray()
    expired = threading.Event()

    def expire():
        expired.set()
        process.kill()

    def drain_errors():
        while chunk := process.stderr.read(65536):
            errors.extend(chunk)
            del errors[:-MAX_STDERR]

    timer = threading.Timer(timeout, expire)
    reader = threading.Thread(target=drain_errors, daemon=True)
    overflow = False
    try:
        timer.start()
        reader.start()
        while chunk := process.stdout.read(65536):
            if len(output) + len(chunk) > max_output:
                overflow = True
                process.kill()
                break
            output.extend(chunk)
        returncode = process.wait(timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        expire()
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(5)
        process.stdout.close()
        process.stderr.close()
    if overflow:
        raise OutputLimitError(f'{cmd[0]} produced more than {max_output} bytes')
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout, bytes(output), bytes(errors))
    result = subprocess.CompletedProcess(cmd, returncode, bytes(output), bytes(errors))
    if check:
        result.check_returncode()
    return result
