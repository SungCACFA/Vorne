import argparse
import sys

from . import create_app

if len(sys.argv) > 1 and sys.argv[1] == "export":
    ap = argparse.ArgumentParser(prog="python -m webapp export",
                                 description="Write every page and period to one self-contained HTML file.")
    ap.add_argument("--out", help="output path (default exports/danbro_<date>_<time>.html)")
    args = ap.parse_args(sys.argv[2:])
    from .export import build

    build(out=args.out)
    sys.exit(0)

ap = argparse.ArgumentParser(prog="python -m webapp")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5000)
ap.add_argument("--debug", action="store_true")
args = ap.parse_args()
create_app().run(host=args.host, port=args.port, debug=args.debug)
