# Build

zsh
```zsh
docker buildx build \
  --build-arg=CACHE_BUST=$(date +%s) \
  --platform linux/amd64 \
  -f docker/Dockerfile \
  -t irfan33/insights:v3-0.1.5 \
  -t irfan33/insights:dev \
  --push .
```