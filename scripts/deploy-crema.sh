#!/usr/bin/env bash
# crema DEV 박스(13.124.110.195) 무중단 배포.
#
# **왜 별도 스크립트인가**: deploy-mq.sh는 소스를 tar로 올려 그 호스트에서 빌드한다. crema
# 박스는 메모리 여유가 1GB 남짓이라 거기서 `uv sync` 빌드를 돌리면 같이 떠 있는 다른 팀
# 컨테이너가 OOM 위험에 들어간다. 그래서 **mq에서 만든 이미지를 옮겨** 띄운다.
#
# **왜 blue/green인가**: 종전엔 `docker rm -f` 뒤 `docker run`이라 그 사이 수 초~십수 초
# 동안 502가 났다(2026-09-03 실측 — 프론트 개발자의 /chat/stream과 로그인 POST가 실제로
# 끊겼다). 헬스 게이트를 앞에 둬도 **마지막 교체 구간**이 남는다. 포트를 둘 두고 Caddy가
# 가리키는 곳만 바꾸면 그 구간이 사라진다 — Caddy reload는 연결을 끊지 않는다.
set -euo pipefail

SSH_HOST="${SSH_HOST:-crema-dev}"
BUILD_HOST="${BUILD_HOST:-mq}"          # 이미지를 만들어 둔 곳
IMAGE="yes24-agent"
CADDYFILE="/root/Caddyfile"
DOMAIN="${DOMAIN:-yes24-agent.dev.griplabs.io}"
PORT_A=8011
PORT_B=8012

echo "[1/5] 활성 포트 확인"
ACTIVE=$(ssh "$SSH_HOST" "sudo awk '/${DOMAIN} \{/,/\}/' $CADDYFILE | grep -oE 'localhost:[0-9]+' | cut -d: -f2")
[ -n "$ACTIVE" ] || { echo "중단: Caddyfile에서 $DOMAIN 블록을 못 찾음"; exit 1; }
if [ "$ACTIVE" = "$PORT_A" ]; then NEW=$PORT_B; else NEW=$PORT_A; fi
echo "  현재 $ACTIVE → 새 $NEW"

echo "[2/5] 이미지 전송 ($BUILD_HOST → $SSH_HOST)"
ssh "$BUILD_HOST" "docker save $IMAGE:latest | gzip -1" | ssh "$SSH_HOST" "gunzip | docker load" | tail -1

echo "[3/5] 새 컨테이너 기동 (:$NEW) — 기존 :$ACTIVE 는 그대로 서비스 중"
ssh "$SSH_HOST" "docker rm -f ${IMAGE}-${NEW} 2>/dev/null || true
docker run -d --name ${IMAGE}-${NEW} -p ${NEW}:8010 \
  --env-file ~/yes24-build-env -v ~/yes24-agent-data:/app/data \
  --log-driver json-file --log-opt max-size=20m --log-opt max-file=3 \
  --restart unless-stopped $IMAGE:latest >/dev/null"

echo "[4/5] 헬스 확인 (최대 60s)"
for i in $(seq 1 12); do
  sleep 5
  H=$(ssh "$SSH_HOST" "curl -s --max-time 4 localhost:${NEW}/health" || true)
  if echo "$H" | grep -q '"status":"ok"'; then echo "  health: $H"; break; fi
  [ "$i" = 12 ] && { echo "  중단: 새 컨테이너가 안 뜬다 — 기존 :$ACTIVE 유지"; ssh "$SSH_HOST" "docker rm -f ${IMAGE}-${NEW}"; exit 1; }
done

echo "[5/5] Caddy 전환 ($ACTIVE → $NEW) + 옛 컨테이너 정리"
ssh "$SSH_HOST" "sudo cp $CADDYFILE ${CADDYFILE}.bak
sudo python3 - <<PY
import re
p='$CADDYFILE'; s=open(p).read()
s=re.sub(r'($DOMAIN \{[^}]*localhost:)[0-9]+', r'\g<1>$NEW', s, count=1)
open(p,'w').write(s)
PY
docker exec caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1
sleep 2
docker rm -f ${IMAGE}-${ACTIVE} ${IMAGE} 2>/dev/null || true
docker image prune -f --filter 'until=24h' >/dev/null 2>&1 || true"
echo "완료. 롤백: Caddyfile을 ${CADDYFILE}.bak 로 되돌리고 caddy reload."
