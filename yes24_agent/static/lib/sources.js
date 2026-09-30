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

// 판형 칩 목록 [{label, url, sale_price, own?}] — 순서는 종이책 → eBook → 크레마클럽, 확인된 것만.
// 카드 자신의 판형도 카드 url·가격으로 넣어(own: true) 판형들이 한 줄에 보이게 한다. 라벨은
// 목록 판형 kind(도서·eBook·외서·클래스24 …) 그대로이고 "도서"만 "종이책"으로 부른다. kind가
// 없으면 is_ebook으로 eBook/종이책을 가리고, 둘 다 침묵이면 자기 칩은 없다. 다른 판형이 하나도
// 없으면 가격 줄과 중복이라 빈 목록이다. 종이책 카드의 eBook 판은 other_formats에 같은 url로
// 이미 있으면 거기 가격을 쓰고, 없을 때만 따로 넣는다. 클럽 칩은 in_cremaclub === true와
// cremaclub_url이 모두 있을 때만(미확인·false는 그리지 않는다) — 최상위 in_cremaclub은 전자책
// 카드의 것, ebook_edition의 것은 종이책 카드가 연 eBook 판의 것이다. 카드 url이 곧 클럽 상세면
// (오리지널 코너 행) 같은 문서라 자기 칩을 생략한다. 클릭 기록의 출처 매칭도 이 목록을 쓴다.
export function editionLinks(src) {
  const kind = typeof src?.kind === "string" && src.kind ? src.kind : null;
  const self = kind ? (kind === "도서" ? "종이책" : kind)
    : src?.is_ebook === true ? "eBook" : src?.is_ebook === false ? "종이책" : null;
  const edition = src?.ebook_edition && typeof src.ebook_edition === "object" ? src.ebook_edition : null;
  const links = (Array.isArray(src?.other_formats) ? src.other_formats : [])
    .filter((f) => f && f.format && isSafeUrl(f.url))
    .map((f) => ({ label: f.format, url: f.url, sale_price: f.sale_price }));
  if (edition && isSafeUrl(edition.url) && !links.some((l) => l.url === edition.url)) {
    links.push({ label: "eBook", url: edition.url });
  }
  const club = src?.in_cremaclub === true ? src : edition;
  const clubUrl = club?.in_cremaclub === true && isSafeUrl(club.cremaclub_url) ? club.cremaclub_url : null;
  if (clubUrl) links.push({ label: "크레마클럽", url: clubUrl });
  if (!links.length) return links;
  if (self && isSafeUrl(src.url) && src.url !== clubUrl) {
    const own = { label: self, url: src.url, sale_price: src.sale_price ?? src.price, own: true };
    // eBook 카드의 other_formats는 종이책 등이라 자기 칩을 그 뒤(클럽 앞)에, 그 밖의 판형은 맨 앞에.
    links.splice(self === "eBook" ? links.length - (clubUrl ? 1 : 0) : 0, 0, own);
  }
  return links;
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
