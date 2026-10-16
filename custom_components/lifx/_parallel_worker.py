"""Establish custom-component imports before starting a spawned LIFX worker."""

from importlib import import_module
import sys

if __name__ == "__main__":
    (
        import_root,
        module_name,
        transport_fields,
        source,
        pipe,
        current_generation,
        stop_event,
    ) = globals()["lifx_worker_args"]
    sys.path.insert(0, import_root)
    parallel = import_module(module_name)
    parallel._worker(  # noqa: SLF001 -- sibling module's subprocess entry point
        parallel.ParallelTransport(*transport_fields),
        source,
        pipe,
        current_generation,
        stop_event,
    )
