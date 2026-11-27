import asyncio, logging, time

from .. import telemetry
from .runner import Settings, serve


def main():
    telemetry.init("loadgen")
    settings = Settings.from_env()
    while True:   # never exits: a crash of the whole loop is logged and the loop starts again
        try:
            asyncio.run(serve(settings))
        except Exception:
            logging.getLogger("loadgen").exception("the load generator stopped; starting again")
            time.sleep(5)


if __name__ == "__main__":
    main()
