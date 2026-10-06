import argparse

from . import create_app

ap = argparse.ArgumentParser(prog="python -m webapp")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=5000)
ap.add_argument("--debug", action="store_true")
args = ap.parse_args()
create_app().run(host=args.host, port=args.port, debug=args.debug)
