#!/usr/bin/env bash
# Build the three images (fakeco-services:dev, fakeco-web:dev,) and save them to .build/fakeco-images.tar.gz,
# for a cluster without a registry (import it with `k3s ctr -n k8s.io images import`).
# DOCKER="sudo docker" when your user may not use the Docker socket.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-docker}
python3 scripts/render_services.py
$DOCKER build -q -t fakeco-services:dev services
$DOCKER build -q -t fakeco-web:dev web
mkdir -p .build
$DOCKER save fakeco-services:dev fakeco-web:dev | gzip -1 > .build/fakeco-images.tar.gz
ls -lh .build/fakeco-images.tar.gz
