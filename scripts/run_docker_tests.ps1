$ErrorActionPreference = "Stop"

$imageName = if ($env:IMAGE_NAME) { $env:IMAGE_NAME } else { "minic-linux-test" }

docker build -f Dockerfile.linux-test -t $imageName .
docker run --rm $imageName
