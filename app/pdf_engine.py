"""Serialize native PDF operations within each server process."""
from functools import wraps
from threading import RLock


_lock = RLock()


def pdf_operation(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)
    return locked
