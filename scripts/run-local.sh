#!/usr/bin/env bash
# Run every service of the demo on this machine, for development (ports: services/fakeco/config.py).
# It needs a Postgres at DATABASE_URL (default postgresql://fakeco:fakeco@127.0.0.1:5432/fakeco) that `python -m fakeco.seed` has filled.
# Nothing is exported to Honeycomb: FAKECO_LOCAL=1 without FAKECO_EXPORT=1 turns the exporters off.
set -u
cd "$(dirname "$0")/../services"
export FAKECO_LOCAL=1
unset FAKECO_EXPORT
PY=${PY:-.venv/bin/python}
trap 'kill 0' EXIT
for s in $($PY -c "from fakeco import config; print(' '.join(config.SERVICES))"); do
  $PY -m fakeco.run "$s" 2>&1 | sed -u "s/^/[$s] /" &
done
if [ "${WITH_LOADGEN:-0}" = 1 ]; then
  FAKECO_API_URL=http://127.0.0.1:9000 $PY -m fakeco.loadgen 2>&1 | sed -u "s/^/[loadgen] /" &
fi
wait
