import time
from contextlib import contextmanager


@contextmanager
def timed_stage(name):
    started = time.perf_counter()
    try:
        yield
    finally:
        print(f"STAGE_TIMING stage={name} seconds={time.perf_counter() - started:.2f}",
              flush=True)
