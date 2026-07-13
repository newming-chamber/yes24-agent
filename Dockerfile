# yes24-agent DEV 배포 이미지 (crema-ai server/Dockerfile 패턴 참고, uv 기반으로 변경)
FROM python:3.10-slim

WORKDIR /app

# curl: 헬스체크용
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# 의존성 레이어 분리 (코드 변경 시 재설치 방지)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY yes24_agent ./yes24_agent

# 세션 sqlite 저장소 — 컨테이너 재시작 간 유지하려면 볼륨 마운트: -v yes24_data:/app/data
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 포트는 config.Settings.port(pydantic-settings)가 단일 출처. PORT 환경변수로 오버라이드
# 가능하게 하고, EXPOSE·HEALTHCHECK·앱 바인딩이 모두 이 값을 참조하게 해 하드코딩을 없앤다.
# (하드코딩 금지 원칙: CLAUDE.md #6. 이전엔 8010이 세 곳에 박혀 settings.port가 무시됐다.)
ENV PORT=8010

EXPOSE ${PORT}

# 셸 형식이라 ${PORT}가 확장된다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

# 시크릿은 이미지에 넣지 않는다 — 런타임 주입: docker run -e GEMINI_API_KEY=... -e PERPLEXITY_API_KEY=...
# workers=1 고정: 공유 클라이언트 싱글턴 + sqlite 세션 DB 특성상 다중 워커는 세션 락 경합 위험 (POC 스코프)
# __main__이 settings.host/port로 uvicorn을 띄우므로 PORT/HOST 환경변수가 그대로 반영된다.
# uv venv에 의존성이 있으므로 bare python이 아닌 `uv run`으로 그 venv의 python을 쓴다.
CMD ["uv", "run", "--no-sync", "python", "-m", "yes24_agent.main"]
