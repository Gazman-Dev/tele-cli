#!/usr/bin/env sh
set -eu

IMAGE_NAME="${IMAGE_NAME:-minic-linux-test}"

docker build -f Dockerfile.linux-test -t "$IMAGE_NAME" .
docker run --rm "$IMAGE_NAME"
