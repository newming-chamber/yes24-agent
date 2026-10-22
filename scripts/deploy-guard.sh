# shellcheck shell=bash
# 배포 공통 가드 — deploy-mq.sh·scripts/deploy-prod.sh·scripts/deploy-crema.sh가 source한다.
# 로직은 여기 한 벌뿐이다(스크립트마다 사본을 두지 않는다). 원격 실행은 호출자가 넘기는
# 명령 접두(`ssh mq`, `ssh_cmd 1.2.3.4` 등) 뒤에 명령 문자열 하나를 붙여 돌린다.
#
# 왜 있는가: 배포 스크립트는 git이 아니라 워킹 디렉터리(또는 이미 만든 이미지)를 싣는다.
#   - 2026-10-07 QA 배포가 다른 세션의 미커밋 파트너 API를 덮어썼고, 2026-10-17 운영 핫픽스는
#     미커밋 상태로 운영에 나가 있었다 — 어느 배포가 무엇을 지웠는지 사후에 알 수 없었다.
#   그래서 세 가지를 막거나 남긴다.
#   (a) 빌드 대상 경로에 미커밋 변경이 있으면 중단(ALLOW_DIRTY=1이면 경고 후 진행).
#   (b) 지금 그 환경에서 떠 있는 컨테이너에 있는 yes24_agent 파일이 새 빌드에 없으면 목록을
#       보이고 중단(ALLOW_REMOVE=1이면 진행) — 다른 작업의 파일을 조용히 지우는 배포를 막는다.
#   (c) 이미지 라벨에 git 커밋 해시와 dirty 여부를 남긴다(`docker inspect`로 컨테이너에서 읽힌다).

# 이미지에 들어가는 경로(Dockerfile의 COPY 대상과 같다). 미커밋 판정은 이 경로만 본다 — 문서·
# 테스트·운영 메모 같은 비배포 파일의 변경으로 배포를 막지 않는다.
GUARD_BUILD_PATHS=(Dockerfile pyproject.toml uv.lock yes24_agent)
GUARD_LABEL_REVISION="org.opencontainers.image.revision"
GUARD_LABEL_DIRTY="io.griplabs.yes24-agent.dirty"

# 파일 목록 정규화: 컨테이너 경로의 /app/ 접두를 떼고, 실행·편집 부산물(바이트코드·macOS 메타
# 파일)을 빼고 정렬한다 — 양쪽 목록이 같은 규칙을 거쳐야 차집합이 코드 파일만 가리킨다.
_guard_normalize() {
  sed 's#^/app/##' | { grep -vE '(^|/)(__pycache__/|\.DS_Store$|\._)|\.pyc$' || true; } \
    | sed '/^$/d' | LC_ALL=C sort -u
}

# 라벨 인자 문자열(커밋 해시·0/1이라 공백이 없다 — 원격 명령 문자열에도 그대로 실린다).
_guard_set_labels() {
  GUARD_LABEL_FLAGS="--label $GUARD_LABEL_REVISION=$GUARD_REVISION --label $GUARD_LABEL_DIRTY=$GUARD_DIRTY"
}

_guard_dirty_gate() {
  if [ "${ALLOW_DIRTY:-}" = 1 ]; then
    echo "  경고: ALLOW_DIRTY=1 — 커밋되지 않은 내용을 그대로 배포한다(라벨 dirty=1)"
  else
    echo "  중단: 커밋되지 않은 내용은 배포하지 않는다 — 커밋하거나 ALLOW_DIRTY=1로 다시 실행"
    exit 1
  fi
}

# (a)+(c) 로컬 빌드 트리. GUARD_REVISION·GUARD_DIRTY·GUARD_LABEL_FLAGS를 정한다.
# 사용: deploy_guard_tree <트리 경로>
deploy_guard_tree() {
  local dir="$1" changes
  GUARD_REVISION="$(git -C "$dir" rev-parse HEAD 2>/dev/null)" \
    || { echo "  중단: $dir 는 git 체크아웃이 아니다 — 배포 커밋을 기록할 수 없다"; exit 1; }
  changes="$(git -C "$dir" status --porcelain --untracked-files=all -- "${GUARD_BUILD_PATHS[@]}")"
  if [ -n "$changes" ]; then
    GUARD_DIRTY=1
    echo "  빌드 대상 경로(${GUARD_BUILD_PATHS[*]})에 미커밋 변경 $(printf '%s\n' "$changes" | wc -l | tr -d ' ')건:"
    printf '%s\n' "$changes" | sed 's/^/    /'
    _guard_dirty_gate
  else
    GUARD_DIRTY=0
  fi
  _guard_set_labels
  echo "  빌드 커밋 ${GUARD_REVISION:0:12} dirty=$GUARD_DIRTY"
}

# (a) 이미 만든 이미지(빌드 없이 옮기거나 다시 띄우는 배포). 라벨이 없는 옛 이미지는 출처를 알 수
# 없으므로 dirty로 본다(fail-closed).
# 사용: deploy_guard_image <이미지> <원격 실행 명령...>
deploy_guard_image() {
  local image="$1"; shift
  local labels
  labels="$("$@" "docker image inspect -f '{{index .Config.Labels \"$GUARD_LABEL_REVISION\"}}|{{index .Config.Labels \"$GUARD_LABEL_DIRTY\"}}' $image")" \
    || { echo "  중단: 이미지 $image 조회 실패"; exit 1; }
  GUARD_REVISION="${labels%%|*}"
  GUARD_DIRTY="${labels##*|}"
  echo "  이미지 $image 커밋 ${GUARD_REVISION:0:12} dirty=$GUARD_DIRTY"
  case "$GUARD_REVISION|$GUARD_DIRTY" in
    *"<no value>"* | "|"* | *"|") echo "  이미지에 커밋 라벨이 없다(가드 이전 빌드) — 출처를 확인할 수 없다"; _guard_dirty_gate ;;
    *"|0") ;;
    *) _guard_dirty_gate ;;
  esac
}

# 새 빌드의 파일 목록 — 로컬 트리에서(이미지 COPY 대상과 같은 yes24_agent/).
deploy_guard_tree_files() {
  (cd "$1" && find yes24_agent -type f) | _guard_normalize
}

# 새 빌드의 파일 목록 — 이미 만든 이미지에서.
# 사용: deploy_guard_image_files <이미지> <원격 실행 명령...>
deploy_guard_image_files() {
  local image="$1"; shift
  "$@" "docker run --rm --no-healthcheck --entrypoint find $image /app/yes24_agent -type f" | _guard_normalize
}

# (b) 이름을 지정한 컨테이너 중 실행 중인 것 전부의 파일 합집합 중 새 목록에 없는 것.
#     이름 목록은 호출자가 교체할 컨테이너 목록과 같은 변수에서 넘긴다 — 접두 필터는 교체
#     집합과 어긋날 수 있어 버렸다(2026-10-20, 운영 서버당 컨테이너 2개 반영).
# 사용: deploy_guard_removed "<새 파일 목록>" "<컨테이너 이름들(공백 구분)>" <원격 실행 명령...>
deploy_guard_removed() {
  local new_files="$1" wanted="$2"; shift 2
  local names name filters="" running="" listed missing
  for name in $wanted; do filters="$filters --filter 'name=^$name\$'"; done
  names="$("$@" "docker ps --format '{{.Names}}'$filters")" \
    || { echo "  중단: 실행 중 컨테이너 조회 실패"; exit 1; }
  if [ -z "$names" ]; then
    echo "  실행 중인 컨테이너($wanted) 없음 — 파일 비교 생략"
    return 0
  fi
  [ -n "$new_files" ] || { echo "  중단: 새 빌드 파일 목록이 비었다"; exit 1; }
  for name in $names; do
    echo "  실행 중 $name: $("$@" "docker inspect -f '커밋 {{index .Config.Labels \"$GUARD_LABEL_REVISION\"}} dirty={{index .Config.Labels \"$GUARD_LABEL_DIRTY\"}}' $name" || true)"
    # 조회 실패를 "파일 없음"으로 접지 않는다 — 비교할 수 없으면 중단한다.
    listed="$("$@" "docker exec $name find /app/yes24_agent -type f" | _guard_normalize)" \
      || { echo "  중단: $name 파일 목록 조회 실패"; exit 1; }
    running="$running$listed"$'\n'
  done
  missing="$(LC_ALL=C comm -23 <(printf '%s' "$running" | _guard_normalize) <(printf '%s\n' "$new_files"))"
  if [ -z "$missing" ]; then
    echo "  실행 중 컨테이너의 파일이 새 빌드에 모두 있다"
    return 0
  fi
  echo "  새 빌드에서 빠지는 파일 $(printf '%s\n' "$missing" | wc -l | tr -d ' ')개(지금 떠 있는 컨테이너에는 있다):"
  printf '%s\n' "$missing" | sed 's/^/    /'
  if [ "${ALLOW_REMOVE:-}" = 1 ]; then
    echo "  경고: ALLOW_REMOVE=1 — 위 파일을 지우는 배포로 진행한다"
  else
    echo "  중단: 다른 작업의 파일을 덮어쓸 수 있다 — 의도한 삭제면 ALLOW_REMOVE=1로 다시 실행"
    exit 1
  fi
}
