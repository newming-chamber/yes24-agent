"""16뷰 RBTI 매트릭스 시뮬레이터 — retrieve/select-once → server-render.

한 질문에 대해 16 RBTI 페르소나의 답을 나란히 전시하는 시뮬레이터(Phase C). 순진한
16×(도구 포함 에이전트 루프)는 Yes24 트래픽 16배·비용 16배라 치명적이므로,
**공유 검색·상세 선택 1회 → 검증된 근거의 16뷰 서버 렌더링**으로 분리한다
(rbti-feature-plan §3.2). 채팅 루프(runner/orchestrator)를 재사용하지 않고 원시 요소
(Yes24Client·parse_search·register_source·validate_citations·persona)만 재사용하는 별도 경로다.
절대 불변식은 채팅과 동일하다: 상품은 Yes24 공유풀에만 근거하고, 인용 id를 검증하며,
16개 유형은 AXIS 곱으로 파생한다.
"""
