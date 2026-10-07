import argparse
import logging
import sys

from . import ROOT, create_app

if len(sys.argv) > 1 and sys.argv[1] == "export":
    ap = argparse.ArgumentParser(prog="python -m webapp export",
                                 description="Write every page and period to one self-contained HTML file.")
    ap.add_argument("--out", help="output path (default exports/danbro_<date>_<time>.html)")
    ap.add_argument("--keep", action="store_true",
                    help="keep earlier exports in the output folder (by default they are deleted)")
    args = ap.parse_args(sys.argv[2:])
    from .export import build

    build(out=args.out, keep=args.keep)
    sys.exit(0)

ap = argparse.ArgumentParser(prog="python -m webapp")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5000)
ap.add_argument("--debug", action="store_true")
ap.add_argument("--no-live-writer", action="store_true",
                help="do not poll the devices for /live (run `python -m xlpipe live` separately instead)")
args = ap.parse_args()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
app = create_app()

# The live writer runs here, in background threads, so there is one process to start. It is started
# by this entry point only - never by create_app(), which tests and the export also call.
lock = None
if not args.no_live_writer:
    from xlpipe.__main__ import AlreadyRunning, live_lock, load_config
    from xlpipe import live

    cfg = load_config(str(ROOT / "config.yaml"))
    try:
        lock = live_lock({**cfg, "cache_path": str(app.config["CACHE_PATH"])}).__enter__()
        interval, timeout = live.settings(cfg)
        live.start(app.config["CACHE_PATH"], cfg["devices"], interval=interval, timeout=timeout)
        logging.info("live writer started: %d devices every %.0f s", len(cfg["devices"]), interval)
    except AlreadyRunning:
        logging.info("a separate live writer is running; /live reads what it writes")

# use_reloader=False: the debug reloader would start a second process, and a second writer.
app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
