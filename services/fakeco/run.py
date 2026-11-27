"""Start one service: python -m fakeco.run <service-name>"""
import importlib, os, sys

from . import config


def main(argv=None):
    argv = argv or sys.argv[1:]
    if len(argv) != 1 or argv[0] not in config.SERVICES:
        sys.exit("usage: python -m fakeco.run <service>\nservices: " + ", ".join(config.SERVICES))
    name = argv[0]
    os.environ["SERVICE_NAME"] = name
    kind, module, port = config.SERVICES[name]
    import uvicorn
    app = importlib.import_module(module).build()
    local = os.environ.get("FAKECO_LOCAL") == "1"
    uvicorn.run(app, host="127.0.0.1" if local else "0.0.0.0", port=port if local else 8080, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
