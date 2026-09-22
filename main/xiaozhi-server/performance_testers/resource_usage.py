import sys

try:
    import resource
except ImportError:
    resource = None


def process_usage():
    if resource is None:
        return None, None

    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss = usage.ru_maxrss
    if sys.platform == "darwin":
        peak_rss /= 1024 * 1024
    else:
        peak_rss /= 1024
    return peak_rss, usage.ru_utime + usage.ru_stime


def print_initialization_usage(duration, before, after):
    print(f"Provider initialization: {duration:.3f}s")
    if after[0] is None:
        return

    print(
        "Peak memory after initialization: "
        f"{after[0]:.1f} MB (+{max(0.0, after[0] - before[0]):.1f} MB)"
    )
    print(f"CPU time during initialization: {after[1] - before[1]:.3f}s")


def print_benchmark_usage(initialized):
    current = process_usage()
    if current[0] is None:
        return

    print(f"Peak process memory: {current[0]:.1f} MB")
    print(f"Benchmark CPU time: {current[1] - initialized[1]:.3f}s")
