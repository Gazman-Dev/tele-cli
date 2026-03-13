$ErrorActionPreference = "Stop"

$imageName = if ($env:IMAGE_NAME) { $env:IMAGE_NAME } else { "tele-cli-linux-test" }

docker build -f Dockerfile.linux-test -t $imageName .
docker run --rm $imageName
