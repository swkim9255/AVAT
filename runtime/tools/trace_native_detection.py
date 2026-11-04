"""Local diagnostic wrapper; never imported by the application."""
import importlib.util
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from investigator.detection_process import DetectionJob

original = DetectionJob.arguments


def traced_arguments(job):
    arguments = original(job)
    return [
        "-u", "-c",
        "import faulthandler,runpy; faulthandler.dump_traceback_later(12); "
        "runpy.run_module('investigator.detection_worker',run_name='__main__')",
        *[argument for argument in arguments[2:] if argument != '--stdin-cancel'],
    ]


def read_stderr(job):
    chunk = bytes(job._process.readAllStandardError())
    job._stderr = (job._stderr + chunk)[-8000:]
    sys.stderr.write(chunk.decode('utf-8', errors='replace'))
    sys.stderr.flush()


DetectionJob.arguments = traced_arguments
DetectionJob._read_stderr = read_stderr
spec = importlib.util.spec_from_file_location('native_smoke', root / 'local_dev/tools/smoke_native_detection_gui.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(module.main())
