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
HOST_PORT="${HOST_PORT:-8010}"      # mq 호스트 포트
# 컨테이너 **내부** 포트. `-p` 매핑과 앱이 실제로 듣는 포트가 반드시 같아야 하는데, 아래에서
# `--env-file`이 Dockerfile의 `ENV PORT`를 **덮는다** — 로컬 .env에 PORT가 있으면(QA 관행:
# qa/grounding_smoke.py가 8016을 권한다) 앱은 컨테이너 안에서 그 포트를 듣고 docker는 8010으로
# 매핑해 **배포가 조용히 죽는다**(health가 영원히 안 뜬다). 종전엔 이 값이 -p 줄에 숫자로 박혀
# 있어 그 어긋남을 막을 방법이 없었다(2026-08-31 적대 감사 지적). 한 변수로 모으고, 아래에서
# 원격 .env에 PORT를 **강제 주입**해 로컬 값이 승계되지 않게 한다 — SERVE_FRONTEND와 같은 패턴.
CONTAINER_PORT="${CONTAINER_PORT:-8010}"
IMAGE="yes24-agent"
TAG="$(date +%Y%m%d-%H%M%S)"        # 롤백용 날짜 태그
# 원격 작업 디렉터리는 홈 하위다. mq는 translator-api·generative-api·rabbitmq가 함께 도는
# 공유 인스턴스라, 시크릿(.env)과 사용자 대화 DB를 world-readable /tmp에 두지 않는다
# (/tmp는 재부팅·systemd-tmpfiles 정리로 조용히 비워지기도 한다). `~`는 로컬에서 펼치지 않고
# 원격 셸이 펼치도록 문자 그대로 넘긴다 — 모든 사용처가 ssh 명령 문자열이라 안전하다.
REMOTE_BUILD="~/yes24-build"
REMOTE_DATA="~/yes24-agent-data"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] 소스 패키징 (시크릿 제외)"
SRC_TGZ="$(mktemp -t yes24-src.XXXX.tgz)"
trap 'rm -f "$SRC_TGZ"' EXIT
tar czf "$SRC_TGZ" -C "$LOCAL_DIR" Dockerfile pyproject.toml uv.lock yes24_agent
if tar tzf "$SRC_TGZ" | grep -qiE '(^|/)\.env'; then echo "중단: tar에 .env 포함됨"; exit 1; fi

echo "[2/6] 소스 전송 → $SSH_HOST"
scp -q "$SRC_TGZ" "$SSH_HOST:/tmp/yes24-src.tgz"

echo "[3/6] 시크릿(.env) 확인 (전송은 빌드 후 — 이미지에는 안 들어감)"
if [ ! -f "$LOCAL_DIR/.env" ]; then echo "중단: 로컬 .env 없음 (GEMINI/PERPLEXITY/TAVILY 키 필요)"; exit 1; fi

echo "[4/6] mq에서 네이티브 x86_64 빌드 (:latest + :$TAG)"
ssh "$SSH_HOST" bash -lc "'
  set -e
  rm -rf $REMOTE_BUILD && mkdir -p $REMOTE_BUILD
  tar xzf /tmp/yes24-src.tgz -C $REMOTE_BUILD
  cd $REMOTE_BUILD
  docker build -t $IMAGE:latest -t $IMAGE:$TAG .
'"
# .env는 빌드 후에 올린다 (COPY 대상 아님 + .dockerignore 제외라 이미지 유출 없음).
# 로컬 .env와 배포 전용 주입값을 **한 스트림으로 stdin 전송**한다:
#   - 값이 ssh argv에 실리지 않아 로컬·원격 `ps`에 평문으로 뜨지 않는다(공유 인스턴스).
#   - 값에 따옴표·공백·개행이 있어도 원격 셸 명령이 깨지지 않는다 — 비밀번호 형식 제약 자체가
#     사라지므로 "단순 문자열 권장" 같은 사용 규칙을 문서로 떠넘길 필요가 없다.
#   - install -m 600으로 원격 파일을 소유자 전용으로 만든다(scp는 로컬 모드 0644를 그대로 옮긴다).
#   - 로컬 .env는 건드리지 않고, 매 배포마다 새로 쓰므로 append 누적도 없다.
# MATRIX_ENABLED: RBTI 16뷰 매트릭스 노출 스위치. 2026-07-28 사용자 결정으로 기본 **노출(true)** —
#   과거 "rbti 제외하고 띄우자"(기본 false) 방침을 뒤집었다. 숨김 배포는 MATRIX_ENABLED=false ./deploy-mq.sh.
# ACCESS_PASSWORD: 공유 패스워드 로그인월 **오버라이드**. 로컬 .env가 통째로 스트림되므로
#   미지정이어도 로컬 .env의 ACCESS_PASSWORD가 그대로 프로드에 승계된다(월 활성) — 이 변수는
#   프로드만 다른 비밀번호를 쓸 때만 필요하다. (과거 "미지정이면 월 비활성" 주석은 오기 —
#   2026-08-24 배포 사고 조사에서 교정.)
# SERVE_FRONTEND: 내장 프론트(UI 페이지·정적 파일·로그인월) 서빙 스위치. **배포 기본은
#   현행 운영 모드인 true**(2026-08-19 사용자 결정 — 데모·로그인월 유지). 과거 기본
#   false(2026-08-12)는 "새 프론트 연동 시 백엔드 전용 전환" 계획의 선반영이었는데, 운영
#   모드와 어긋난 기본값이 무언 배포에서 데모를 404로 만든 사고(2026-08-24)로 뒤집었다.
#   백엔드 전용 전환은 그때 SERVE_FRONTEND=false ./deploy-mq.sh로 명시한다.
# SESSION_FALLBACK_ALLOWED: 세션 DB 생성 실패 시 InMemory 폴백 허용 여부. 배포 세션 DB는
#   네트워크 MySQL이라 조용한 폴백은 "영속 중이라 믿는 비영속"(대화가 재시작마다 증발,
#   admin·집계는 위장 정상)이 된다 — 배포 기본은 false(기동 실패로 즉시 드러낸다).
# LOG_FILE_PATH: 파일 로깅. 코드(RotatingFileHandler)는 있는데 배포가 값을 안 넣어 stdout만
#   남고 있었다 — 컨테이너를 재기동하면 그전 로그가 사라져 사후 추적 근거가 없다(2026-09-02
#   확인). 호스트에 마운트되는 /app/data 아래에 둬야 재기동·재배포를 넘어 남는다.
LOG_FILE_PATH="${LOG_FILE_PATH:-/app/data/yes24-agent.log}"
MATRIX_ENABLED="${MATRIX_ENABLED:-true}"
SERVE_FRONTEND="${SERVE_FRONTEND:-true}"
SESSION_FALLBACK_ALLOWED="${SESSION_FALLBACK_ALLOWED:-false}"
{
  cat "$LOCAL_DIR/.env"
  printf '\nMATRIX_ENABLED=%s\n' "$MATRIX_ENABLED"
  printf 'SERVE_FRONTEND=%s\n' "$SERVE_FRONTEND"
  printf 'SESSION_FALLBACK_ALLOWED=%s\n' "$SESSION_FALLBACK_ALLOWED"
  printf 'PORT=%s\n' "$CONTAINER_PORT"
  printf 'LOG_FILE_PATH=%s\n' "$LOG_FILE_PATH"
  if [ -n "${ACCESS_PASSWORD:-}" ]; then printf 'ACCESS_PASSWORD=%s\n' "$ACCESS_PASSWORD"; fi
} | ssh "$SSH_HOST" "install -m 600 /dev/stdin $REMOTE_BUILD/.env"
echo "  → 원격 .env 전송(모드 600) · MATRIX_ENABLED=$MATRIX_ENABLED · SERVE_FRONTEND=$SERVE_FRONTEND · SESSION_FALLBACK_ALLOWED=$SESSION_FALLBACK_ALLOWED · PORT=$CONTAINER_PORT · LOG_FILE_PATH=$LOG_FILE_PATH 주입(로컬 .env 불변)"
if [ -n "${ACCESS_PASSWORD:-}" ]; then
  echo "  → ACCESS_PASSWORD 오버라이드 주입(로그인월 이 값으로 활성)"
else
  echo "  → ACCESS_PASSWORD 오버라이드 없음 — 로컬 .env 값 승계(있으면 월 활성)"
fi

echo "[5/6] 컨테이너 기동 (포트 $HOST_PORT, sqlite 바인드마운트, restart=unless-stopped)"
ssh "$SSH_HOST" bash -lc "'
  set -e
  install -d -m 700 $REMOTE_DATA
  docker rm -f yes24-agent 2>/dev/null || true
  docker run -d --name yes24-agent \
    -p $HOST_PORT:$CONTAINER_PORT \
    --env-file $REMOTE_BUILD/.env \
    -v $REMOTE_DATA:/app/data \
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
