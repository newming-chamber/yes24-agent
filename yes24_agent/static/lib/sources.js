// 출처 페이로드 유틸 — 두 페이지 공용. coverUrl이 갈라져 있던 자리다(채팅은 평면 image_url만,
// 매트릭스는 meta.image_url만 읽어 서로 다른 출처에서만 표지가 떴다). 여기서 둘 다 관대하게 읽는다.

export const WEB_TYPES = new Set(["web"]);

// 표지 URL은 두 형태로 온다: 스트리밍 source 이벤트는 평면 image_url, done.sources는 meta.image_url.
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
