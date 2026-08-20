#!/usr/bin/env bash
#
# Build the Frappe v16 Insights image from the current branch HEAD.
#
#   ./docker/build-v16.sh                 # build tag v16-0.1.0
#   ./docker/build-v16.sh v16-0.2.0       # build a specific image tag
#   MEM=4g ./docker/build-v16.sh          # override the build memory budget
#   SKIP_PUSH=1 ./docker/build-v16.sh     # rebuild from the pin as it already is
#
# WHY THIS IS NOT JUST `docker build`
#
# docker/apps.json names a GIT TAG, and `bench init` clones that tag from GitHub.
# The image therefore contains what is PUSHED, never what is in this working
# tree. Running docker build without moving the tag first silently produces an
# image of whatever code the tag pointed at last time. That is the single most
# expensive mistake available here -- a 25 minute build of the wrong commit.
#
# So the order is: commit -> push branch -> move tag -> push tag -> build.
#
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE_TAG="${1:-v16-0.1.0}"
IMAGE="irfan33/insights:${IMAGE_TAG}"
MEM="${MEM:-6g}"
CPUS="${CPUS:-0-3}"
REMOTE="${REMOTE:-work}"

PIN_REF=$(python3 -c "import json;print(json.load(open('docker/apps.json'))[0]['branch'])")
PIN_URL=$(python3 -c "import json;print(json.load(open('docker/apps.json'))[0]['url'])")
BRANCH=$(git rev-parse --abbrev-ref HEAD)

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- 1. refuse to build a tree that is not what you think it is ---------------
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "ERROR: uncommitted changes. The image is built from a pushed tag, so"
  echo "       anything not committed will NOT be in it. Commit or stash first:"
  echo
  git status --short --untracked-files=no | sed 's/^/         /'
  exit 1
fi

HEAD_SHA=$(git rev-parse HEAD)
say "branch ${BRANCH} @ ${HEAD_SHA:0:8}  ->  image ${IMAGE}"

# --- 2. publish the code and move the pin ------------------------------------
# The pin in apps.json may be either a branch or a tag; bench clones with
# `git clone --branch <ref>`, which accepts both (but never a bare SHA).
#   branch pin -> pushing the branch is enough, nothing else to move.
#   tag pin    -> the tag has to be dragged to HEAD or the image is built from
#                 whatever commit it pointed at last time.
if git ls-remote --exit-code --heads "$PIN_URL" "$PIN_REF" >/dev/null 2>&1; then
  PIN_KIND=branch
elif git ls-remote --exit-code --tags "$PIN_URL" "$PIN_REF" >/dev/null 2>&1; then
  PIN_KIND=tag
else
  PIN_KIND=unknown
fi

if [ "${SKIP_PUSH:-0}" != "1" ]; then
  say "pushing ${BRANCH} to ${REMOTE} (pin ${PIN_REF} is a ${PIN_KIND})"
  git push "$REMOTE" "$BRANCH"
  if [ "$PIN_KIND" = "tag" ]; then
    say "repointing tag ${PIN_REF} to HEAD"
    git tag -f -a "$PIN_REF" -m "Insights v3 + MCP, pinned for the Frappe v16 image"
    git push --force "$REMOTE" "$PIN_REF"
  elif [ "$PIN_KIND" = "branch" ] && [ "$PIN_REF" != "$BRANCH" ]; then
    echo "WARNING: apps.json pins branch '${PIN_REF}' but you are on '${BRANCH}'."
    echo "         Pushing '${BRANCH}' does not move '${PIN_REF}'."
  fi
fi

# --- 3. resolve what the image will ACTUALLY clone ----------------------------
# ^{} dereferences an annotated tag to its commit.
PIN_SHA=$(git ls-remote "$PIN_URL" "refs/tags/${PIN_REF}^{}" | cut -f1)
[ -n "$PIN_SHA" ] || PIN_SHA=$(git ls-remote "$PIN_URL" "refs/tags/${PIN_REF}" | cut -f1)
[ -n "$PIN_SHA" ] || PIN_SHA=$(git ls-remote "$PIN_URL" "refs/heads/${PIN_REF}" | cut -f1)
if [ -z "$PIN_SHA" ]; then
  echo "ERROR: ${PIN_REF} does not resolve in ${PIN_URL}"; exit 1
fi

say "pin ${PIN_REF} resolves to ${PIN_SHA:0:8}"
if [ "$PIN_SHA" != "$HEAD_SHA" ]; then
  echo "WARNING: the pin is NOT your local HEAD (${HEAD_SHA:0:8})."
  echo "         The image will contain ${PIN_SHA:0:8}. Re-run without SKIP_PUSH=1"
  echo "         if that is not what you want."
fi

# --- 4. build ----------------------------------------------------------------
# Classic builder, not buildx: buildx is not installed here AND it rejects
# --memory. This host runs four compose stacks in 16 GB with swap chronically
# full, so the build is fenced. --memory-swap equal to --memory disables swap
# for the build: an over-budget build then dies as a contained exit 137 instead
# of dragging the whole machine into the global OOM killer.
#
# Reading the failure:
#   exit 137 "Killed"                -> the CGROUP was too small. Raise MEM.
#   exit 134 "mark-compacts near
#             heap limit"            -> V8 was too small. Raise
#                                       --max-old-space-size in
#                                       frontend/package.json (currently 4096).
# They point in OPPOSITE directions. Read the code before changing anything.
#
# CACHE_BUST is the resolved commit, so an unchanged pin reuses the bench init
# layer and a moved pin forces a rebuild. Keying it on a timestamp would rebuild
# always; keying it on apps.json contents would never rebuild, because the
# filename is identical when the tag is repointed.
say "building ${IMAGE}  (mem=${MEM}, cpus=${CPUS})"
DOCKER_BUILDKIT=0 docker build \
  --file docker/Dockerfile \
  --memory="$MEM" \
  --memory-swap="$MEM" \
  --cpuset-cpus="$CPUS" \
  --build-arg CACHE_BUST="$PIN_SHA" \
  --tag "$IMAGE" \
  .

# --- 5. prove the build is what you asked for --------------------------------
say "verifying"
docker run --rm "$IMAGE" bash -c '
  env/bin/python -V
  env/bin/python -c "import frappe; print(\"frappe\", frappe.__version__)"
  node -v
  test -f apps/insights/insights/www/insights.html \
    && echo "frontend assets: present ($(ls sites/assets/insights/frontend/assets | wc -l) files)" \
    || echo "frontend assets: MISSING -- the vite build silently no-opd"
  ls apps/insights/insights/mcp/ >/dev/null 2>&1 \
    && echo "mcp module: present" || echo "mcp module: MISSING"
'

cat <<EOF

Built ${IMAGE} from ${PIN_SHA:0:8}

Next:
  Deploy locally   sudo sed -i 's/^CUSTOM_TAG=.*/CUSTOM_TAG=${IMAGE_TAG}/' \\
                     /www/server/panel/data/compose/insights_v16/.env
                   sudo docker compose -p insights_v16 \\
                     -f /www/server/panel/data/compose/insights_v16/docker-compose.yaml \\
                     --env-file /www/server/panel/data/compose/insights_v16/.env up -d
                   sudo docker exec insights_v16-backend-1 bench --site frontend migrate

  Push to Hub      docker push ${IMAGE}
EOF
