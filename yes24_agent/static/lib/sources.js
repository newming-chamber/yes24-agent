// 출처 페이로드 유틸 — 두 페이지 공용. 현재 공개 DTO의 평면 image_url을 우선하고,
// 기존 세션 스냅샷의 meta.image_url도 관대하게 읽는다.

export const WEB_TYPES = new Set(["web"]);

// 신규 공개 DTO는 평면 image_url이고, meta.image_url은 기존 스냅샷 호환 경로다.
// http(s)만 허용(javascript: 등 위험 스킴 차단).
export function coverUrl(src) {
  const u = src && (src.image_url || (src.meta && src.meta.image_url));
  return typeof u === "string" && /^https?:\/\//i.test(u) ? u : null;
}

// 출처 링크 열기 — http(s) 스킴만. 외부 url(web_search)에 javascript:가 섞여도 실행되지 않게.
export function safeOpen(url) {
  if (typeof url === "string" && /^https?:\/\//i.test(url)) window.open(url, "_blank", "noopener");
}
export function isSafeUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}
