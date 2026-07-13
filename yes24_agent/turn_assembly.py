"""done.text 조립 — 문장 분할·프리앰블 재진술 dedup·문단/항목 dedup·턴 병합.

`runner.py`에서 function-call 루프의 턴별 텍스트를 사용자에게 보일 최종 본문으로
조립하는 순수 함수들만 추출한 모듈이다(동작 불변, ADK 비의존). pro 계열 모델이 도구
호출 사이 각 턴을 처음부터 다시 생성하며 되풀이하는 선두 프리앰블·문단·항목 반복을,
서로 다른 책·문장은 보존하면서 중복만 접는다.
"""

import re
from difflib import SequenceMatcher


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


# 답변 본문(리스트·인용) 시작을 알리는 마커: [n] 인용, 줄머리 불릿/번호/헤딩.
# 이 마커가 없는 텍스트는 "순수 안내/공감 산문"(프리앰블)으로 취급한다.
_ANSWER_MARKER = re.compile(r"\[\d+\]|^\s*(?:[*\-•]|#{1,6}|\d+\.)\s", re.MULTILINE)

# 재진술 프리앰블 판정 임계(near-dup). 실측 보정값:
# 드롭 대상 A(패러프레이즈 프리앰블) ratio≈0.53 / 선두 공유 토큰 3개.
# 보존 대상 flash(공감→검색, 재진술 아님) ratio≈0.34, boilerplate만 공유하는 다른 주제
# 프리앰블("위로 책"→"재테크 책") ratio≈0.63이나 선두 토큰 0개 — 둘의 교집합을 이중
# 게이트(선두 토큰 공유 + 전체 유사도)로 갈라 오탐을 없앤다. 유사도만/토큰만으로는 분리 불가.
_PREAMBLE_SIMILARITY_MIN = 0.45
_PREAMBLE_SHARED_LEADING_MIN = 2

_WORD = re.compile(r"[가-힣]+|[a-zA-Z]+|\d+")


def _leading_prose(text: str) -> str:
    """텍스트에서 첫 답변 마커([n]·불릿·헤딩) 이전의 선두 산문만 반환한다.

    마커가 없으면 전체가 선두 산문. 프리앰블 재진술 판정은 이 선두 산문끼리만 비교해,
    책 목록·인용 본문을 비교 대상에서 배제한다.
    """
    m = _ANSWER_MARKER.search(text)
    return text[: m.start()] if m else text


def _shared_leading_tokens(a: str, b: str) -> int:
    """두 문자열의 선두 토큰이 몇 개나 연속 일치하는지 센다(같은 도입부인지)."""
    ta, tb = _WORD.findall(a), _WORD.findall(b)
    n = 0
    for x, y in zip(ta, tb):
        if x != y:
            break
        n += 1
    return n


def _is_restated_preamble(earlier: str, later_lead: str) -> bool:
    """earlier(순수 프리앰블 턴)가 later_lead(뒤 턴 선두 산문)에서 재진술됐는지 판정.

    같은 도입부(선두 토큰 공유)이면서 전체적으로 충분히 유사할 때만 True. 두 조건을
    모두 요구해, 재진술이 아닌 서로 다른 프리앰블(boilerplate만 공유)의 오탐을 막는다.
    완전 동일 반복은 물론, 어미·표현만 바꾼 패러프레이즈까지 잡되 주제가 다르면 보존한다.
    """
    a, b = earlier.strip(), later_lead.strip()
    if not a or not b:
        return False
    if _shared_leading_tokens(a, b) < _PREAMBLE_SHARED_LEADING_MIN:
        return False
    return SequenceMatcher(None, a, b).ratio() >= _PREAMBLE_SIMILARITY_MIN


def _drop_restated_pure_preambles(turns: list[str]) -> list[str]:
    """뒤 턴에서 재진술되는 '순수 프리앰블 턴'(답변 마커가 전혀 없는 안내/공감 산문)을
    제거한다.

    pro 계열 모델은 도구 호출 전 공감/안내 문장만 담은 턴을 흘린 뒤, 도구 결과 후 턴에서
    같은 프리앰블을 어미·표현만 바꿔 재진술한다(문장 단위 정확 일치가 아니라 선두-문장
    prefix dedup으로는 안 잡힘). 순수 프리앰블 턴은 답변 콘텐츠(책·인용)를 담지 않으므로,
    뒤 턴이 그 도입부를 재진술하면 앞의 홀로 선 프리앰블은 잉여다 → 드롭한다.

    재진술이 아니면(flash: 공감→바로 답변) 보존해 '도구 호출 전 공감' 계약을 지킨다.
    답변 마커를 가진 턴은 콘텐츠 유실 위험이 있어 절대 통째로 드롭하지 않는다(선두-문장
    prefix dedup이 뒤에서 처리).
    """
    survivors: list[str] = []
    for i, turn in enumerate(turns):
        if not _ANSWER_MARKER.search(turn):
            later_leads = (_leading_prose(turns[j]) for j in range(i + 1, len(turns)))
            if any(_is_restated_preamble(turn, lead) for lead in later_leads):
                continue
        survivors.append(turn)
    return survivors


_LIST_ITEM = re.compile(r"^\s*[*\-•]\s")
_CITATION = re.compile(r"\[(\d+)\]")
# 순번 목록 마커("1." "2)" 등)만으로 이뤄진 조각. _split_sentences는 "1. 본문"을 "1."과
# "본문"으로 쪼개는데, 이 순번 마커는 산문이 아니므로 프리앰블 문장 dedup(2단계) 대상에서
# 뺀다 — 안 그러면 두 목록(PC/모바일 절차)의 같은 번호가 앞 목록과 겹쳐 통째로 사라진다(실측).
_ORDINAL_MARKER = re.compile(r"^\s*\d+[.)]\s*$")
# 같은 인용 id를 가진 두 불릿 블록을 '같은 항목의 재초안'으로 볼 문자열 유사도 하한. 이 미만이면
# 서로 다른 항목이 같은 단일 출처를 인용한 것으로 보고 둘 다 보존한다(정책 답변은 모든 항목이
# 같은 공지[2]를 인용하므로, id만으로 dedup하면 서로 다른 규정 항목이 사라진다 — 실측 회귀).
_LIST_ITEM_DUP_RATIO = 0.6


def _citation_ids(text: str) -> frozenset[int]:
    """텍스트의 [n] 인용 번호 집합. 책 항목의 동일성 판정 키로 쓴다."""
    return frozenset(int(n) for n in _CITATION.findall(text))


def _dedup_repeats(text: str) -> str:
    """턴 병합 후에도 남는 '문단·항목 단위 의미상 동일 반복'을 제거한다.

    pro 계열 모델은 도구 호출 사이 답변을 여러 번 초안→확장하며, 같은 책 항목을 두 번
    싣거나(예: 설명만 → 가격까지) 같은 프리앰블 문장을 재진술한다. 턴이 구분자 없이
    이어붙으면 선두-문장 prefix dedup으로는 이런 mid-block 반복을 못 잡는다. 두 단계로 없앤다:

    1) 책 항목 dedup: 줄머리 불릿으로 시작하는 리스트 항목 블록(\\n\\n 구분) 중 **같은
       인용 id 집합**을 가지면서 **뒤 블록과 텍스트가 유사한** 것만 마지막(가장 완성된 초안)만
       남긴다. 유사도 조건이 없으면, 정책 답변처럼 서로 다른 규정 항목이 모두 같은 단일 공지[2]를
       인용할 때 그 항목들이 통째로 사라진다(실측 회귀) — id만으로는 '같은 책 재초안'과 '한 출처를
       공유하는 서로 다른 항목'을 못 가르므로 유사도로 가른다. 서로 다른 책(다른 id)은 불변.
    2) 프리앰블 문장 dedup: 인용도 불릿도 순번 마커도 없는 **순수 산문 문장**이 앞에서 이미
       나온 것과 정확히 동일하면 뒤 반복을 제거한다(keep-first). 인용·리스트·순번 마커는 건드리지
       않아 책 설명·가격·목록 번호는 보존하고, 공감/안내 프리앰블의 재등장만 접는다.
    """
    # 1) 같은 인용 id + 텍스트 유사한 리스트 항목은 마지막 것만 남긴다.
    parts = re.split(r"(\n{2,})", text)
    units: list[list[str]] = []  # [블록, 뒤따르는 구분자]
    for i in range(0, len(parts), 2):
        block = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        units.append([block, sep])

    last_item_index: dict[frozenset[int], int] = {}
    last_block_by_ids: dict[frozenset[int], str] = {}
    for idx, (block, _) in enumerate(units):
        if _LIST_ITEM.match(block):
            ids = _citation_ids(block)
            if ids:
                last_item_index[ids] = idx
                last_block_by_ids[ids] = block

    kept: list[list[str]] = []
    for idx, unit in enumerate(units):
        block = unit[0]
        if _LIST_ITEM.match(block):
            ids = _citation_ids(block)
            if ids and last_item_index[ids] != idx:
                # 뒤(최종) 블록과 유사할 때만 '같은 항목 재초안'으로 보고 드롭한다. 유사하지
                # 않으면(같은 출처를 인용하는 서로 다른 정책 항목) 보존한다.
                ratio = SequenceMatcher(
                    None, block.strip(), last_block_by_ids[ids].strip()
                ).ratio()
                if ratio >= _LIST_ITEM_DUP_RATIO:
                    continue
        kept.append(unit)

    rebuilt: list[str] = []
    for j, (block, sep) in enumerate(kept):
        rebuilt.append(block)
        if j < len(kept) - 1:
            rebuilt.append(sep or "\n\n")
    deduped = "".join(rebuilt)

    # 2) 마커 없는 프리앰블 문장의 정확 반복 제거(keep-first). 순번 항목(마커 "1."·그 본문)은
    #    산문이 아니라 목록 콘텐츠라 제외한다 — 두 목록(PC/모바일 절차)이 같은 순번·같은 단계를
    #    공유하는 건 정상이므로, 마커만 제외하면 본문이 겹쳐 지워져 순번만 빈 채 남는다(실측).
    seen: set[str] = set()
    out: list[str] = []
    prev_ordinal = False  # 직전(비공백) 조각이 순번 마커였는지 — 그 뒤 본문은 목록 항목 본문.
    for piece in _split_sentences(deduped):
        norm = piece.strip()
        is_ordinal = _ORDINAL_MARKER.match(piece) is not None
        # 순번 마커·순번 항목 본문·불릿·인용 문장은 dedup 대상에서 뺀다(목록 콘텐츠 보존).
        markerless = (
            "[" not in piece
            and _LIST_ITEM.match(piece) is None
            and not is_ordinal
            and not prev_ordinal
        )
        if norm and markerless:
            if norm in seen:
                continue  # 빈 조각(공백·개행)은 prev_ordinal을 이어가도록 아래 갱신을 건너뛴다.
            seen.add(norm)
        out.append(piece)
        # 순번 마커면 True, 실질 콘텐츠면 False. 빈 조각(공백·개행)은 직전 상태를 유지한다
        # (마커와 본문 사이 개행 조각이 prev_ordinal을 꺼뜨리지 않게).
        if norm:
            prev_ordinal = is_ordinal
    return "".join(out)


def _merge_restated_turns(turns: list[str]) -> str:
    """function-call 루프의 턴별 텍스트를 이어붙이되, 각 턴이 앞부분에서 되풀이한
    선두 프리앰블(재진술)을 제거하고, 남은 문단·항목 반복까지 접는다.

    현행 루트 에이전트 모델(pro 계열)은 도구 호출 사이 각 LLM 턴을 **처음부터 다시
    생성**하며, 공감 프리앰블 등 앞 턴의 선두 문장을 재진술한다(관측: 프리앰블 2~3중 반복).
    3단계로 중복만 없애고 콘텐츠·정상 반복은 보존한다:

    1) 순수 프리앰블 턴 near-dup 드롭(_drop_restated_pure_preambles): 어미·표현만 바꾼
       패러프레이즈 재진술까지 잡는다(선두 토큰 공유 + 유사도 이중 게이트, 오탐 0).
    2) 선두-문장 prefix dedup: 남은 턴들에서 이미 누적된 본문의 **선두와 정확히 일치하는
       앞 문장들만** 잘라낸다(정확 반복 제거, 리스트·다른 문장은 보존).
    3) 문단·항목 dedup(_dedup_repeats): 턴이 구분자 없이 이어붙어 prefix dedup을 빠져나간
       mid-block 반복(같은 책 항목 재등장, 같은 프리앰블 문장 재진술)을 제거한다.

    flash처럼 턴이 겹침 없이 이어지면 모든 단계가 무동작 → 원문 그대로(도구 호출 전
    프리앰블 포함) 잇는다. 어느 단계도 서로 다른 책·서로 다른 문장(정상 리스트)은 삭제하지 않는다.
    """
    acc: list[str] = []
    acc_norm: list[str] = []
    for turn in _drop_restated_pure_preambles(turns):
        if not turn.strip():
            continue
        pieces = _split_sentences(turn)
        norm = [p.strip() for p in pieces]
        # 누적 본문의 선두와 이 턴의 선두가 문장 단위로 일치하는 개수(빈 조각은 경계).
        k = 0
        while (
            k < len(norm)
            and k < len(acc_norm)
            and norm[k] != ""
            and norm[k] == acc_norm[k]
        ):
            k += 1
        acc.extend(pieces[k:])
        acc_norm.extend(norm[k:])
    return _dedup_repeats("".join(acc))
