import sys


def _out(level, message):
    print(f"{level} {message}", file=sys.stderr, flush=True)


def info(message):
    _out("INFO", message)


def warning(message):
    _out("WARNING", message)


def error(message):
    _out("ERROR", message)


def banner(lines):
    bar = "!" * 72
    for line in (bar, *lines, bar):
        _out("WARNING", line)
