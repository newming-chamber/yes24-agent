// 출처 페이로드·표기 유틸 — 채팅·매트릭스 공용 단일 구현(사본으로 갈라지면 한쪽만 고쳐진다:
// 가격의 "원" 접미가 실제로 갈라져 있었다). 공개 DTO의 평면 image_url을 우선하고, 기존 세션
// 스냅샷의 meta.image_url도 관대하게 읽는다.

export const WEB_TYPES = new Set(["web"]);

export const CARD_LABELS = Object.freeze({ book: "도서", document: "문서", link: "웹" });

export function sourceCardType(src) {
  return Object.hasOwn(CARD_LABELS, src?.card_type) ? src.card_type
    : src?.type === "notice" ? "document" : "link";
}

export function sourceDomain(src) {
  try { return new URL(src?.url).hostname; } catch { return ""; }
}

export function sourceTitle(src) {
  return (typeof src?.title === "string" ? src.title.trim() : "") || sourceDomain(src) || "자료";
}

export function textOffset(text, offset, unit = "unicode_codepoint") {
  if (!Number.isInteger(offset) || offset <= 0) return 0;
  if (unit === "utf16") return Math.min(offset, text.length);
  let count = 0;
  let index = 0;
  for (const point of text) {
    if (count++ >= offset) break;
    index += point.length;
  }
  return index;
}

// 신규 공개 DTO는 평면 image_url이고, meta.image_url은 기존 스냅샷 호환 경로다.
// http(s)만 허용(javascript: 등 위험 스킴 차단).
export function coverUrl(src) {
  const u = src && (src.image_url || (src.meta && src.meta.image_url));
  return typeof u === "string" && /^https?:\/\//i.test(u) ? u : null;
}

// 출처 링크 열기 — http(s) 스킴만. 외부 url(web_search)에 javascript:가 섞여도 실행되지 않게.
export function safeOpen(url) {
  if (typeof url === "string" && /^https?:\/\//i.test(url)) window.open(url, "_blank", "noopener,noreferrer");
}
export function isSafeUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

// 판매가 표기 — sale_price가 정본, 평면 price는 개명 전 스냅샷 호환 폴백. 숫자면 천단위
// 콤마+"원", 문자열이면 그대로 둔다("원"을 덧붙이면 "12,000원원"). 표기할 값이 없으면 null.
export function formatPrice(src) {
  const raw = src && [src.sale_price, src.price].find((v) => v != null && v !== "");
  if (raw == null) return null;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n.toLocaleString("ko-KR") + "원" : String(raw);
}

// 표지 <img> — loading=lazy 금지(스크롤 없는 뷰포트에 JS로 삽입된 이미지는 IntersectionObserver가
// 안 걸려 로드가 멈춘다, Chrome 쿼크·matrix-ux 실측). 실패 시 기본은 자기 제거라 깨진 아이콘
// 대신 본문만 남고, 부모째 지워야 하면 onError로 넘긴다. src는 핸들러 등록 뒤에 설정한다.
export function makeCoverImg(url, className, { onLoad, onError } = {}) {
  const img = document.createElement("img");
  img.className = className;
  img.decoding = "async";
  img.alt = "";
  if (onLoad) img.addEventListener("load", onLoad);
  img.addEventListener("error", onError || (() => img.remove()));
  img.src = url;
  return img;
}

// 컴포저 밖 타이핑 → 포커스만 입력창으로 옮기고 글자 삽입은 브라우저 기본 동작에 맡긴다(문자를
// 직접 넣는 릴레이는 공백·문장부호·Enter·한글 IME 조합을 흘린다). 비프린터블(Enter·화살표)은
// 릴레이하지 않고, blocked()가 참이면 개입하지 않는다 — 모달·팝오버의 자체 키 조작이 우선일 때.
export function relayTypingFocus(target, blocked) {
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const el = e.target;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
    if (target.disabled || (blocked && blocked())) return;
    // 프린터블 1글자(공백·문장부호 포함) 또는 IME 조합 키(한글 첫 타, e.key="Process").
    if (e.key.length === 1 || e.key === "Process") target.focus();
  });
}

// "이 오버뷰 done을 화면에 낼 수 있는가"의 **단일 판정**. 종전엔 단일 패널이 cited_ids 개수로,
// 비교 팔이 sources 개수로 같은 것을 각각 판정했다(2026-08-31 적대 감사). 서버가 sources를
// 인용분으로 필터하니 지금은 대체로 일치하지만, 한쪽 계약이 바뀌면 조용히 갈리는 이중 기준이다.
// 기준은 **인용된 출처가 하나라도 있는가**다 — 마커를 렌더할 근거가 그것뿐이다.
export function overviewIsRenderable(done) {
  const overview = done && done.overview;
  if (!overview || done.degraded) return false;
  // sources와 cited_ids는 **같은 cited-only 집합**이다(postprocess.build_done_payload:572-578 —
  // ordered_sources는 인용분 한정이고 cited_ids는 used_source_ids ∩ by_id). 그래서 둘 중
  // 무엇을 세도 같아야 하고, 합집합을 취하면 계약이 깨진 payload를 조용히 통과시킨다.
  // 명시 필드를 우선하고 없을 때만 sources로 갈음한다(느슨해지지 않게).
  if (Array.isArray(overview.cited_ids)) return overview.cited_ids.length > 0;
  return Array.isArray(overview.sources) && overview.sources.length > 0;
}
