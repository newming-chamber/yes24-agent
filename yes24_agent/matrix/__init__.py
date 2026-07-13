"""16뷰 RBTI 매트릭스 시뮬레이터 — retrieve-once → fan-out-generate.

한 질문에 대해 16 RBTI 페르소나의 답을 나란히 전시하는 시뮬레이터(Phase C). 순진한
16×(도구 포함 에이전트 루프)는 Yes24 트래픽 16배·비용 16배라 치명적이므로,
**공유 검색 1회(retrieval) → 경량 flash 생성 16회(generate)** 로 분리한다
(rbti-feature-plan §3.2). 채팅 루프(runner/orchestrator)를 재사용하지 않고 원시 요소
(Yes24Client·parse_search·register_source·validate_citations·product_gate·persona)만
재사용하는 별도 경로다. 절대 불변식은 채팅과 동일: 상품=Yes24 공유풀 출처만·인용 검증·
cited-fabricated 차단·오늘 시제·16을 케이스로 박지 않음(AXIS 곱).
"""
