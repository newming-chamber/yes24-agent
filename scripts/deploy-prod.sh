#!/usr/bin/env bash
# yes24-agent 운영 배포 — crema-ai 운영 EC2 2대(ALB `Crema-AI` 뒤, arm64 t4g.medium) 롤링.
#
# 왜 이 구조인가 (2026-09-22, yes24-agent.griplabs.io를 mq 단일 박스에서 여기로 옮기며):
#   - 운영 2대는 arm64라 로컬 맥(arm64)에서 buildx로 **네이티브 빌드**해 ECR에 올린다.
#     mq(x86_64) 네이티브 빌드 경로(deploy-mq.sh)는 이 서버들엔 맞지 않는다.
#   - 라우팅은 Caddy가 아니라 ALB다: 443 리스너의 host-header 규칙(yes24-agent.griplabs.io)
#     → 타깃그룹 `Yes24-Agent`(:8010, /health, lb_cookie 고정 세션). 무중단은 서버 단위로
#     타깃그룹에서 빼고(drain 30s) 교체 후 다시 넣는 방식이다(crema-ai deploy.sh와 같은 결).
#   - 고정 세션(stickiness)을 켠 이유: 세션 직렬화 락이 프로세스 내부라(runner.py
#     `_get_session_lock`), 같은 세션의 연속 턴이 두 서버로 갈라지면 락이 걸리지 않는다.
#   - 시크릿은 이미지에 굽지 않는다. 로컬 .env + 배포 주입값을 stdin 한 스트림으로 보내
#     원격 ~/yes24-agent/.env(600)를 매번 새로 쓴다(deploy-mq.sh와 같은 방식).
#
# 사용:  ./scripts/deploy-prod.sh            # 빌드+푸시+롤링
#        SKIP_BUILD=1 ./scripts/deploy-prod.sh   # ECR :latest 그대로 롤링만
#        SERVE_FRONTEND=false ./scripts/deploy-prod.sh   # 백엔드 전용
#
# 롤백:  IMAGE_TAG=<이전 날짜태그> SKIP_BUILD=1 ./scripts/deploy-prod.sh
#
# 주의:  이 스크립트는 git이 아니라 **실행한 체크아웃의 워킹 디렉터리**를 이미지로 만든다.
#        공유 워킹트리에 남의 미커밋 작업이 있으면 그대로 나간다 — 격리 worktree에서 실행한다.
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 인프라 식별자(ECR·타깃그룹 ARN·서버 IP·SSH 키)는 저장소 밖 파일에서 읽는다 — 저장소가 공개라
# 계정 번호·서버 주소를 코드에 두지 않는다(2026-09-28). 형식은 파일 첫 줄 주석 참고.
DEPLOY_CONFIG="${DEPLOY_CONFIG:-$LOCAL_DIR/.deploy-prod.env}"
[ -f "$DEPLOY_CONFIG" ] || { echo "중단: $DEPLOY_CONFIG 없음 (ECR·TG_ARN·PROD_SERVERS·DEPLOY_SSH_KEY)"; exit 1; }
# shellcheck disable=SC1090
set -a; . "$DEPLOY_CONFIG"; set +a
REGION="${REGION:-ap-northeast-2}"
REPO="${REPO:-crema-ai/yes24-agent}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
: "${ECR:?}" "${TG_ARN:?}" "${PROD_SERVERS:?}" "${DEPLOY_SSH_KEY:?}"
KEY="$DEPLOY_SSH_KEY"
read -r -a PROD_SERVERS <<< "$PROD_SERVERS"
CONTAINER="yes24-agent"
CONTAINER_PORT=8010
HOST_PORT=8010
REMOTE_DIR="~/yes24-agent"
REMOTE_DATA="~/yes24-agent-data"
export AWS_PAGER=""

# 배포 주입값 — 의미는 deploy-mq.sh 주석이 정본. COOKIE_SECURE는 ALB가 TLS를 종단하므로 켠다
# (docs/known-limitations.md "cookie_secure=false가 기본" 항목).
LOG_FILE_PATH="${LOG_FILE_PATH:-/app/data/yes24-agent.log}"
# 16뷰 매트릭스(한 요청에 LLM 16회)는 개발자 검증 뷰라 운영에서는 끈다(2026-09-28).
MATRIX_ENABLED="${MATRIX_ENABLED:-false}"
SERVE_FRONTEND="${SERVE_FRONTEND:-true}"
SESSION_FALLBACK_ALLOWED="${SESSION_FALLBACK_ALLOWED:-false}"
COOKIE_SECURE="${COOKIE_SECURE:-true}"

ssh_cmd() { ssh -o StrictHostKeyChecking=no -i "$KEY" "ubuntu@$1" "$2"; }

# 운영 설정은 로컬 개발용 .env가 아니라 운영 전용 파일에서 읽는다(2026-09-28 — 로컬 .env를
# 그대로 복사해 운영이 dev DB·로컬 실험 키·개발 인증 통로를 쓰던 것을 끊는다).
ENV_FILE="${ENV_FILE:-$LOCAL_DIR/.env.prod}"
[ -f "$ENV_FILE" ] || { echo "중단: $ENV_FILE 없음 (운영 전용 설정)"; exit 1; }
grep -q '^SESSION_DB_URL=.*/yes24_agent$' "$ENV_FILE" || { echo "중단: $ENV_FILE의 SESSION_DB_URL이 운영 DB(yes24_agent)가 아님"; exit 1; }

if [ -z "${SKIP_BUILD:-}" ]; then
  TS_TAG="$(date +%Y%m%d-%H%M%S)"
  echo "[1/4] arm64 빌드 + ECR 푸시 (:latest + :$TS_TAG)"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR" >/dev/null
  docker buildx build --builder multiarch --platform linux/arm64 \
    -t "$ECR/$REPO:latest" -t "$ECR/$REPO:$TS_TAG" --push "$LOCAL_DIR"
  IMAGE_TAG=latest
  echo "  롤백 태그: $TS_TAG"
else
  echo "[1/4] 빌드 생략 — ECR $REPO:$IMAGE_TAG 사용"
fi

echo "[2/4] 원격 .env 전송 (600) + 이미지 pre-pull"
for entry in "${PROD_SERVERS[@]}"; do
  ip="${entry%%:*}"
  {
    # 로컬 개발용 값은 운영에 싣지 않는다 — DEV_API_KEY(`dev-admin`)는 회원 조회 없이 인증을
    # 통과시키는 개발 통로라, 공개 도메인에 실리면 그 문자열만으로 인증이 뚫린다(2026-09-28).
    grep -vE '^[[:space:]]*DEV_API_KEY=' "$ENV_FILE"
    printf '\nMATRIX_ENABLED=%s\nSERVE_FRONTEND=%s\nSESSION_FALLBACK_ALLOWED=%s\nPORT=%s\nLOG_FILE_PATH=%s\nCOOKIE_SECURE=%s\n' \
      "$MATRIX_ENABLED" "$SERVE_FRONTEND" "$SESSION_FALLBACK_ALLOWED" "$CONTAINER_PORT" "$LOG_FILE_PATH" "$COOKIE_SECURE"
  } | ssh_cmd "$ip" "install -d -m 700 $REMOTE_DIR $REMOTE_DATA && install -m 600 /dev/stdin $REMOTE_DIR/.env"
  ssh_cmd "$ip" "aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR >/dev/null 2>&1 && docker pull -q $ECR/$REPO:$IMAGE_TAG" &
done
wait

echo "[3/4] 서버별 롤링 교체 (ALB 제외 → drain → 교체 → health → ALB 복귀)"
for entry in "${PROD_SERVERS[@]}"; do
  ip="${entry%%:*}"; iid="${entry##*:}"
  echo "  -- $ip ($iid)"
  aws elbv2 deregister-targets --target-group-arn "$TG_ARN" --targets Id="$iid",Port=$HOST_PORT --region "$REGION"
  sleep 35
  ssh_cmd "$ip" "docker rm -f $CONTAINER >/dev/null 2>&1 || true
docker run -d --name $CONTAINER -p $HOST_PORT:$CONTAINER_PORT \
  --env-file $REMOTE_DIR/.env -v $REMOTE_DATA:/app/data \
  --log-driver json-file --log-opt max-size=50m --log-opt max-file=3 \
  --restart unless-stopped $ECR/$REPO:$IMAGE_TAG >/dev/null"
  ok=""
  for i in $(seq 1 12); do
    sleep 5
    if ssh_cmd "$ip" "curl -fsS --max-time 4 localhost:$HOST_PORT/health" 2>/dev/null | grep -q '"status":"ok"'; then ok=1; break; fi
  done
  [ -n "$ok" ] || { echo "  중단: $ip 컨테이너가 안 뜬다 — ALB에서 빠진 상태로 둔다(다른 서버가 서비스 중)"; exit 1; }
  aws elbv2 register-targets --target-group-arn "$TG_ARN" --targets Id="$iid",Port=$HOST_PORT --region "$REGION"
  for i in $(seq 1 12); do
    sleep 10
    st=$(aws elbv2 describe-target-health --target-group-arn "$TG_ARN" --targets Id="$iid",Port=$HOST_PORT --region "$REGION" --query 'TargetHealthDescriptions[0].TargetHealth.State' --output text)
    [ "$st" = "healthy" ] && { echo "  ALB healthy"; break; }
  done
  [ "${st:-}" = "healthy" ] || { echo "  중단: ALB에서 healthy가 안 된다 ($st)"; exit 1; }
  ssh_cmd "$ip" "docker image prune -af --filter 'until=24h' >/dev/null 2>&1 || true"
done

echo "[4/4] 완료 — 확인: curl -s https://yes24-agent.griplabs.io/health"
