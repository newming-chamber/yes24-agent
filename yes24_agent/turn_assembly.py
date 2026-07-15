"""done.text 조립 — 문장 분할·ack 절단·항목 재초안 dedup.

`runner.py`·`orchestrator.py`가 LLM 턴 텍스트를 사용자에게 보일 최종 본문으로 조립할 때 쓰는
순수 함수들만 모은 모듈이다(ADK 비의존).

**done.text는 단일 턴 텍스트다**(팀 결정, 2026-07-14). 도구 호출 전 텍스트는 진행 발화이므로
ack 채널로 빠지고 본문에 담기지 않는다 — 즉 조립기가 받는 턴은 하나뿐이다. 예전에는 턴들을
이어붙이며 '앞 턴 재진술'을 걷어내는 크로스턴 dedup 계층이 있었으나, ack 채널이 도구 전 텍스트를
분리하면서 그 입력 자체가 사라져 삭제했다(그 계층은 이득 없이 정상 본문을 지우는 회귀만 냈다).
"""

import re
from difflib import SequenceMatcher

from yes24_agent.postprocess import cited_ids


def _event_text(event) -> str:
    """이벤트 content에서 텍스트 파트만 이어붙인다(function_call 파트는 text=None)."""
    if not event.content or not event.content.parts:
        return ""
    return "".join(part.text or "" for part in event.content.parts)


# 문장 경계 분할용. 마침표·느낌표·물음표 뒤가 공백이거나 한글이면(공백 누락 문장 이음)
# 경계로 본다 — 숫자 사이 소수점(2.5)·영문 약어(U.S)는 뒤가 공백/한글이 아니라 분할되지 않는다.
# 캡처 그룹이라 re.split 결과가 [본문, 구분자, 본문, …]로 원문을 손실 없이 재구성한다.
_SENTENCE_BOUNDARY = re.compile(r"((?:[.!?]+)(?=\s|[가-힣])|\n+)")


def _split_sentences(text: str) -> list[str]:
    """text를 문장 조각 리스트로 분할한다("".join(결과) == text 불변식 유지).

    각 조각은 문장 본문 + 뒤따르는 구분자(마침표·개행)를 포함해, 다시 이어붙이면
    공백·개행·서식이 원문 그대로 복원된다.
    """
    raw = _SENTENCE_BOUNDARY.split(text)
    pieces: list[str] = []
    for i in range(0, len(raw), 2):
        seg = raw[i]
        delim = raw[i + 1] if i + 1 < len(raw) else ""
        piece = seg + delim
        if piece:
            pieces.append(piece)
    return pieces


def extract_ack(preamble: str, max_chars: int) -> tuple[str, str]:
    """도구 전 preamble에서 인터스티셜 응대(ack)로 방출할 앞부분과 나머지를 나눈다.

    문장 경계로만 자른다(중간 절단 없음) — 최소 첫 문장을 담고, 다음 문장을 더해도 max_chars를
    넘지 않는 한 이어 담다가 상한에 닿으면 멈춘다. 나머지(remainder)는 문장 경계에서 시작하므로
    이후 홀드 경로에서 flush돼도 어색하지 않다. `(ack, remainder)`를 돌려주며, 이어붙이면 원문이다.
    """
    pieces = _split_sentences(preamble)
    if not pieces:
        return "", preamble
    ack_pieces: list[str] = []
    total = 0
    for piece in pieces:
        if ack_pieces and total + len(piece) > max_chars:
            break
        ack_pieces.append(piece)
        total += len(piece)
        if total >= max_chars:
            break
    ack = "".join(ack_pieces)
    remainder = "".join(pieces[len(ack_pieces):])
    return ack, remainder


_LIST_ITEM = re.compile(r"^\s*[*\-•]\s")
# 같은 인용 id를 가진 두 불릿 블록을 '같은 항목의 재초안'으로 볼 문자열 유사도 하한. 이 미만이면
# 서로 다른 항목이 같은 단일 출처를 인용한 것으로 보고 둘 다 보존한다(정책 답변은 모든 항목이
# 같은 공지[2]를 인용하므로, id만으로 dedup하면 서로 다른 규정 항목이 사라진다 — 실측 회귀).
_LIST_ITEM_DUP_RATIO = 0.6


def _merge_restated_turns(turns: list[str]) -> str:
    """턴 텍스트를 본문으로 조립하고, 같은 항목의 재초안 블록만 접는다.

    모델이 답변을 초안→확장하며 같은 항목을 두 번 실을 수 있다(예: 설명만 → 가격까지).
    줄머리 불릿으로 시작하는 리스트 항목 블록(빈 줄 구분) 중 **같은 인용 id 집합**을 가지면서
    **마지막 블록과 텍스트가 유사한** 것만 드롭해 마지막(가장 완성된) 판본을 남긴다.

    유사도 조건이 없으면, 정책 답변처럼 서로 다른 규정 항목이 모두 같은 단일 공지[2]를 인용할 때
    그 항목들이 통째로 사라진다(실측 회귀) — id만으로는 '같은 항목 재초안'과 '한 출처를 공유하는
    서로 다른 항목'을 못 가르므로 유사도로 가른다. 서로 다른 항목(다른 id)·같은 문장의 정상 반복
    (항목마다 붙는 맺음말, 두 절차 목록이 공유하는 같은 단계)은 손대지 않는다.
    """
    text = "".join(turns)
    parts = re.split(r"(\n{2,})", text)
    units: list[tuple[str, str]] = []  # (블록, 뒤따르는 구분자)
    for i in range(0, len(parts), 2):
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        units.append((parts[i], sep))

    last_index: dict[frozenset[int], int] = {}
    for idx, (block, _) in enumerate(units):
        if _LIST_ITEM.match(block):
            ids = frozenset(cited_ids(block))
            if ids:
                last_index[ids] = idx

    kept: list[tuple[str, str]] = []
    for idx, (block, sep) in enumerate(units):
        if _LIST_ITEM.match(block):
            ids = frozenset(cited_ids(block))
            final_idx = last_index.get(ids)
            if ids and final_idx != idx:
                final_block = units[final_idx][0]
                ratio = SequenceMatcher(None, block.strip(), final_block.strip()).ratio()
                if ratio >= _LIST_ITEM_DUP_RATIO:
                    continue
        kept.append((block, sep))

    rebuilt: list[str] = []
    for j, (block, sep) in enumerate(kept):
        rebuilt.append(block)
        if j < len(kept) - 1:
            rebuilt.append(sep or "\n\n")
    return "".join(rebuilt)
