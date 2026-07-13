#!/usr/bin/env bash
# yes24-agent → mq 인스턴스 경량 배포 스크립트
#
# 왜 이 방식인가 (검증 근거, 2026-07-09):
#   - mq는 x86_64(ip-172-31-7-18, 43.202.241.71), 로컬 맥은 arm64.
#     로컬에서 amd64 크로스빌드(buildx)는 QEMU 에뮬레이션에서 `uv sync`가
#     segfault(exit 139)로 실패 → 신뢰 불가. 그래서 **mq에서 네이티브 빌드**한다.
#   - 리포는 아직 커밋 전(작업트리 배포)이라 git clone 대신 소스 tar를 전송한다.
#   - Dockerfile이 COPY하는 파일만 전송(Dockerfile pyproject.toml uv.lock yes24_agent/) → ~295KB.
#   - 시크릿은 이미지에 굽지 않는다. .env를 mq로 따로 전송해 `--env-file`로 런타임 주입.
#
# 포트: mq에서 8010/8011 모두 free 확인됨(기존: translator-api:30001, generative-api:50100,
#       rabbitmq:5672/15672). 기본 HOST_PORT=8010. 필요시 환경변수로 override.
#
# 사용:  ./deploy-mq.sh            # 빌드+기동
#        HOST_PORT=8011 ./deploy-mq.sh
#
# 롤백:  ssh mq 'docker rm -f yes24-agent && docker run -d ... yes24-agent:<이전_날짜태그>'
set -euo pipefail

SSH_HOST="${SSH_HOST:-mq}"          # ~/.ssh/config의 host 별칭 (User/HostName/IdentityFile 포함)
HOST_PORT="${HOST_PORT:-8010}"      # mq 호스트 포트 (컨테이너 내부는 8010 고정)
IMAGE="yes24-agent"
TAG="$(date +%Y%m%d-%H%M%S)"        # 롤백용 날짜 태그
REMOTE_BUILD="/tmp/yes24-build"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] 소스 패키징 (시크릿 제외)"
SRC_TGZ="$(mktemp -t yes24-src.XXXX.tgz)"
trap 'rm -f "$SRC_TGZ"' EXIT
tar czf "$SRC_TGZ" -C "$LOCAL_DIR" Dockerfile pyproject.toml uv.lock yes24_agent
if tar tzf "$SRC_TGZ" | grep -qiE '(^|/)\.env'; then echo "중단: tar에 .env 포함됨"; exit 1; fi

echo "[2/6] 소스 전송 → $SSH_HOST"
scp -q "$SRC_TGZ" "$SSH_HOST:/tmp/yes24-src.tgz"

echo "[3/6] 시크릿(.env) 전송 → $SSH_HOST:$REMOTE_BUILD/.env (이미지에는 안 들어감)"
if [ ! -f "$LOCAL_DIR/.env" ]; then echo "중단: 로컬 .env 없음 (GEMINI/PERPLEXITY/TAVILY 키 필요)"; exit 1; fi

echo "[4/6] mq에서 네이티브 x86_64 빌드 (:latest + :$TAG)"
ssh "$SSH_HOST" bash -lc "'
  set -e
  rm -rf $REMOTE_BUILD && mkdir -p $REMOTE_BUILD
  tar xzf /tmp/yes24-src.tgz -C $REMOTE_BUILD
  cd $REMOTE_BUILD
  docker build -t $IMAGE:latest -t $IMAGE:$TAG .
'"
# .env는 빌드 후에 올린다 (COPY 대상 아님 + .dockerignore 제외라 이미지 유출 없음)
scp -q "$LOCAL_DIR/.env" "$SSH_HOST:$REMOTE_BUILD/.env"

# RBTI 16뷰 매트릭스는 프로드에서 숨긴다("rbti 제외하고 띄우자"). **원격 .env 복사본에만**
# MATRIX_ENABLED를 주입한다 — 로컬 .env는 건드리지 않아 로컬 개발은 계속 매트릭스가 보인다.
# 원격 .env는 매 배포 로컬본으로 새로 덮이므로(위 scp) append가 누적되지 않는다. 노출 배포가
# 필요하면 MATRIX_ENABLED=true ./deploy-mq.sh (기본 false).
MATRIX_ENABLED="${MATRIX_ENABLED:-false}"
ssh "$SSH_HOST" bash -lc "'printf \"\nMATRIX_ENABLED=%s\n\" \"$MATRIX_ENABLED\" >> $REMOTE_BUILD/.env'"
echo "  → 원격 .env에 MATRIX_ENABLED=$MATRIX_ENABLED 주입(로컬 .env 불변)"

# 공유 패스워드 로그인월. ACCESS_PASSWORD가 주어졌을 때만 **원격 .env 복사본에** 주입한다
# (미지정이면 월 비활성 — 로컬 .env는 손대지 않음). 예: ACCESS_PASSWORD=비번 ./deploy-mq.sh
# (MATRIX_ENABLED과 동일 패턴. 비밀번호는 따옴표·개행 없는 단순 문자열 권장.)
if [ -n "${ACCESS_PASSWORD:-}" ]; then
  ssh "$SSH_HOST" bash -lc "'printf \"\nACCESS_PASSWORD=%s\n\" \"$ACCESS_PASSWORD\" >> $REMOTE_BUILD/.env'"
  echo "  → 원격 .env에 ACCESS_PASSWORD 주입(로그인월 활성, 로컬 .env 불변)"
else
  echo "  → ACCESS_PASSWORD 미지정 — 로그인월 비활성"
fi

echo "[5/6] 컨테이너 기동 (포트 $HOST_PORT, sqlite 바인드마운트, restart=unless-stopped)"
ssh "$SSH_HOST" bash -lc "'
  set -e
  mkdir -p /tmp/yes24-agent-data
  docker rm -f yes24-agent 2>/dev/null || true
  docker run -d --name yes24-agent \
    -p $HOST_PORT:8010 \
    --env-file $REMOTE_BUILD/.env \
    -v /tmp/yes24-agent-data:/app/data \
    --log-driver json-file --log-opt max-size=50m --log-opt max-file=3 \
    --restart unless-stopped \
    $IMAGE:latest
'"

echo "[6/6] 헬스체크 (최대 20s 대기)"
ssh "$SSH_HOST" bash -lc "'
  for i in \$(seq 1 20); do
    s=\$(curl -fsS http://localhost:$HOST_PORT/health 2>/dev/null) && { echo \"health: \$s\"; break; }
    sleep 1
  done
  docker inspect --format \"{{json .State.Health}}\" yes24-agent
'"
echo "완료. 태그 $IMAGE:$TAG (롤백용). 로그: ssh $SSH_HOST 'docker logs -f --tail 200 yes24-agent'"
