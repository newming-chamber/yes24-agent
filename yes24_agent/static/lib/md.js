// 마크다운 최소 파서 + 인용 마커 렌더 — 채팅(index)·매트릭스(matrix) 공용 단일 구현.
// 두 페이지에 복제돼 있던 파서가 이미 갈라졌던 자리다(마커 승격·공백 규칙이 한쪽에만 있었음).
// 여기가 유일한 사본이다. innerHTML 금지(XSS) — 텍스트 노드/요소로만 조립한다.
//
// 블록: 헤더(#{1,6})와 파이프 테이블만 승격하고 나머지는 인라인(볼드·마커) 평문(pre-wrap).
// 마커: 대괄호 숫자가 곧 인용은 아니다 — isCitation(id)이 참인 id만 배지로 승격하고, 아니면
// 평문으로 둔다(연도 [2024]·수량 [1,000] 오탐 차단). 백엔드가 마커를 출처와 대조해 검증하는
// 것과 같은 원리를 렌더에도 적용한다.

const MARKER_RE = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
const BOLD_RE = /\*\*([\s\S]+?)\*\*/g;
const MD_H_RE = /^(#{1,6})\s+(.*)$/;
const MD_PIPE_RE = /^\s*\|.*\|\s*$/;          // |로 시작·끝나는 라인만 표 후보
const MD_SEP_RE = /^\s*\|?[\s:|-]+\|?\s*$/;   // 구분선 |---|:--:|
// 리스트 라인: 불릿(-·*·•) 또는 번호(1. / 1)). 캡처1=마커 종류(순서형은 숫자), 캡처2=내용.
// 마커 뒤 공백 1칸 이상을 요구해 "*강조*"·"1.5" 같은 비리스트 라인을 배제한다.
const MD_BULLET_RE = /^\s*[-*•]\s+(.*)$/;
const MD_ORDERED_RE = /^\s*(\d+)[.)]\s+(.*)$/;
// 코드 — 서버 postprocess의 코드/프로즈 분할을 렌더에 미러링한다: 코드 안 [n]은 인용이 아니라
// 리터럴이라 배지로 승격하지 않는다(예: `print(data[1])`의 [1]은 그대로). 코드가 최우선.
const MD_FENCE_RE = /^\s*(```|~~~)/;      // 코드펜스 여닫이(``` 또는 ~~~)
const MD_INLINE_CODE_RE = /`([^`]+)`/g;   // 인라인 `코드`
// 표 셀 줄바꿈 — 마크다운 표는 셀 안에서 개행을 못 써서 모델이 관례적으로 <br>을 쓴다.
// 이것만 실제 줄바꿈으로 승격한다(다른 태그는 문자 그대로). 화이트리스트 1종.
const MD_CELL_BR_RE = /<br\s*\/?>/gi;

// 마커 칩 주변 공백 정리(문자 목록이 아니라 규칙 두 개):
//  1) 칩은 앞말에 붙는다 — 마커 바로 앞의 공백(줄바꿈 제외)은 접는다.
//  2) 칩 뒤의 닫는·종결 구두점은 문장에 붙는다 — 마커 직후 "공백 + 구두점"의 공백을 접는다.
//     구두점은 유니코드 부류(Po 일반구두점·Pe 닫는괄호·Pf 닫는따옴표)로 판정한다.
const LEAD_WS_RE = /[^\S\n]+$/;
const TAIL_WS_RE = /^[^\S\n]+(?=[\p{Po}\p{Pe}\p{Pf}])/u;

function appendTextSlice(target, slice, afterMarker, beforeMarker) {
  let s = slice;
  if (afterMarker) s = s.replace(TAIL_WS_RE, "");
  if (beforeMarker) s = s.replace(LEAD_WS_RE, "");
  if (s) target.appendChild(document.createTextNode(s));
}

// 마커는 url을 알면 **하이퍼링크**, 모르면 기존 칩이다. 두 갈래 모두 class="marker"라
// CSS·간격 규칙은 하나로 유지된다. url 검증(스킴 화이트리스트)은 호출부가 소유한다 —
// opts.citationUrl은 이미 안전한 url이거나 빈 문자열을 돌려주기로 한 계약이다
// (index.html은 lib/sources.js의 isSafeUrl을 쓴다. 여기에 정규식을 복제하지 않는다).
function makeMarker(sid, opts) {
  const href = opts.citationUrl ? opts.citationUrl(sid) : "";
  // 앵커는 포커스·Enter·새 창을 네이티브로 준다 — tabIndex·role·keydown 수동 배선 불필요.
  // 칩 폴백(url 없음)만 버튼 시맨틱을 갖는다.
  const badge = document.createElement(href ? "a" : "sup");
  badge.className = "marker";
  badge.textContent = sid;
  if (href) {
    badge.href = href;
    badge.target = "_blank";
    badge.rel = "noopener noreferrer";
    badge.setAttribute("aria-label", "출처 " + sid + " 열기 (새 창)");
    // 이동은 기본 동작에 맡기고, 같은 클릭으로 출처 카드도 강조한다(마커↔카드 연동 보존).
    badge.addEventListener("click", (e) => {
      e.stopPropagation();
      if (opts.onMarker) opts.onMarker(sid);
    });
    return badge;
  }
  badge.tabIndex = 0;
  badge.setAttribute("role", "button");
  badge.setAttribute("aria-label", "출처 " + sid + " 보기");
  const fire = (e) => { e.stopPropagation(); if (opts.onMarker) opts.onMarker(sid); };
  badge.addEventListener("click", fire);
  badge.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire(e); }
  });
  return badge;
}

function renderMarkersInto(target, text, opts) {
  const re = new RegExp(MARKER_RE.source, "g");
  let last = 0, m, afterMarker = false;
  while ((m = re.exec(text)) !== null) {
    const ids = m[1].split(",").map((s) => s.trim()).filter(Boolean);
    if (!ids.length || !ids.every((sid) => opts.isCitation(sid))) continue; // 평문으로 남긴다
    appendTextSlice(target, text.slice(last, m.index), afterMarker, true);
    ids.forEach((sid) => target.appendChild(makeMarker(sid, opts)));
    last = re.lastIndex;
    afterMarker = true;
  }
  appendTextSlice(target, text.slice(last), afterMarker, false);
}

// 인라인: **볼드**를 top-level로 분리한다(볼드가 `코드`를 감싸는 흔한 경우 — "**A `x` B**" —
// 를 깨지 않게). 각 구간(볼드 포함) 안에서 `코드`를 떼고, 코드 밖에서만 마커를 조립한다.
function renderInlineInto(target, text, opts) {
  const re = new RegExp(BOLD_RE.source, "g");
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) renderCodeInto(target, text.slice(last, m.index), opts);
    const strong = document.createElement("strong");
    renderCodeInto(strong, m[1], opts);
    target.appendChild(strong);
    last = re.lastIndex;
  }
  if (last < text.length) renderCodeInto(target, text.slice(last), opts);
}

// `코드` 구간을 리터럴(<code>, 마커 승격 없음)로 떼고, 코드 밖에서만 마커를 조립한다.
// 코드 안 [n]은 인용이 아니라 문자 그대로다(예: `print(data[1])`의 [1]).
function renderCodeInto(target, text, opts) {
  const re = new RegExp(MD_INLINE_CODE_RE.source, "g");
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) renderMarkersInto(target, text.slice(last, m.index), opts);
    const code = document.createElement("code");
    code.className = "md-code";
    code.textContent = m[1]; // 리터럴 — 배지 승격 안 함
    target.appendChild(code);
    last = re.lastIndex;
  }
  if (last < text.length) renderMarkersInto(target, text.slice(last), opts);
}

// 표 셀 — <br>을 실제 줄바꿈(<br> 요소)으로 바꾸고 각 조각은 평소대로 인라인 렌더한다.
// 요소를 직접 만들 뿐 innerHTML을 쓰지 않아 임의 HTML 삽입 경로가 없다(<br> 외엔 문자 그대로).
function renderCellInto(target, text, opts) {
  // 모델이 한 셀에 여러 항목을 넣으려 <li>/<ul> HTML을 쓰는 경우가 있다. 렌더러는
  // innerHTML을 쓰지 않아 그대로 두면 생 태그가 노출되므로(2026-07-24 실측), 리스트
  // 태그를 줄바꿈 경계로 정규화하고 <li>는 불릿을 앞에 단다. 표 마크업이 아닌 인라인
  // HTML 일반을 파싱하는 게 아니라, 셀 안 리스트 관용만 평문 불릿으로 환원한다.
  const normalized = String(text)
    .replace(/<\/?(?:ul|ol)\s*>/gi, "")
    .replace(/<li\s*>/gi, "<br>• ")
    .replace(/<\/li\s*>/gi, "")
    .replace(/^<br>/i, "");
  const parts = normalized.split(MD_CELL_BR_RE);
  parts.forEach((part, i) => {
    if (i) target.appendChild(document.createElement("br"));
    renderInlineInto(target, part, opts);
  });
}

/**
 * 이 텍스트 뒤에서 본문을 **두 블록으로 쪼개도 렌더가 같은가**.
 * 블록 분할은 렌더 단위를 나누므로, 여닫이가 걸친 구조 안에서 쪼개면 양쪽이 깨진다
 * (열린 펜스는 앞 블록 <pre>가 뒤를 못 삼키고, 표 중간이면 뒤 행이 생 파이프로 샌다).
 * 판정은 이 파일이 이미 가진 라인 술어(MD_FENCE_RE·MD_PIPE_RE)를 그대로 쓴다 —
 * 렌더러와 같은 눈으로 봐야 렌더 결과와 어긋나지 않는다. 사례 분기·키워드 목록 없음.
 */
export function canSplitAfter(text) {
  let fences = 0;
  let lastContent = "";
  for (const line of String(text || "").split("\n")) {
    if (MD_FENCE_RE.test(line)) fences++;
    if (line.trim()) lastContent = line;
  }
  if (fences % 2) return false;              // 펜스가 열린 채다
  return !MD_PIPE_RE.test(lastContent);      // 마지막 내용 줄이 표 행이면 표 안이다
}

function splitCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
}

/**
 * 본문을 container에 렌더한다(기존 내용은 지운다).
 * opts.isCitation(id)  → 그 id가 이번 턴 출처인가(배지 승격 조건). 기본: 승격 없음.
 * opts.onMarker(id)    → 배지 클릭·Enter 시 호출.
 * opts.citationUrl(id) → 그 id의 **검증된 안전한** url(없으면 ""). 있으면 마커가 하이퍼링크,
 *                        없으면 기존 칩. 기본: 링크 없음(매트릭스는 이 옵션을 주지 않는다).
 */
export function renderBody(container, text, opts = {}) {
  const o = {
    isCitation: opts.isCitation || (() => false),
    onMarker: opts.onMarker || null,
    citationUrl: opts.citationUrl || null,
  };
  container.textContent = "";
  const lines = (text || "").split("\n");
  let buf = [];
  const flush = () => {
    // 블록 요소가 자체 마진을 가지므로 세그먼트 가장자리 빈 줄은 접는다(이중 공백 방지).
    while (buf.length && !buf[0].trim()) buf.shift();
    while (buf.length && !buf[buf.length - 1].trim()) buf.pop();
    if (buf.length) renderInlineInto(container, buf.join("\n"), o);
    buf = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // 코드펜스(``` / ~~~) 블록 — 안쪽은 리터럴로 <pre><code>에 담는다(마커·볼드·표·리스트 승격
    // 없음). 닫는 펜스 전에 스트림이 끊긴 상태면 온 만큼만 렌더(다음 델타에서 이어짐).
    if (MD_FENCE_RE.test(line)) {
      flush();
      const fence = line.match(MD_FENCE_RE)[1];
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith(fence)) { codeLines.push(lines[i]); i++; }
      const pre = document.createElement("pre");
      pre.className = "md-pre";
      const code = document.createElement("code");
      code.textContent = codeLines.join("\n");
      pre.appendChild(code);
      container.appendChild(pre);
      continue; // 루프의 i++가 닫는 펜스 줄을 건너뛴다
    }
    const hm = line.match(MD_H_RE);
    if (hm) {
      flush();
      const h = document.createElement("div");
      h.className = "md-h " + (hm[1].length <= 3 ? "l3" : "l4");
      renderInlineInto(h, hm[2], o);
      container.appendChild(h);
      continue;
    }
    // 표 = 연속 파이프 라인 2줄 이상(스트리밍 중 첫 줄만 온 상태는 평문 유지).
    if (MD_PIPE_RE.test(line) && MD_PIPE_RE.test(lines[i + 1] || "")) {
      flush();
      const rows = [];
      while (i < lines.length && MD_PIPE_RE.test(lines[i])) { rows.push(lines[i]); i++; }
      i--;
      const wrap = document.createElement("div");
      wrap.className = "md-table-wrap";
      const table = document.createElement("table");
      table.className = "md-table";
      let start = 0;
      if (rows.length >= 2 && MD_SEP_RE.test(rows[1])) {
        const tr = document.createElement("tr");
        for (const c of splitCells(rows[0])) {
          const th = document.createElement("th");
          renderCellInto(th, c, o);
          tr.appendChild(th);
        }
        table.appendChild(tr);
        start = 2;
      }
      for (let r = start; r < rows.length; r++) {
        if (MD_SEP_RE.test(rows[r])) continue; // 표 중간 구분선은 표시 안 함
        const tr = document.createElement("tr");
        for (const c of splitCells(rows[r])) {
          const td = document.createElement("td");
          renderCellInto(td, c, o);
          tr.appendChild(td);
        }
        table.appendChild(tr);
      }
      wrap.appendChild(table);
      container.appendChild(wrap);
      continue;
    }
    // 리스트 = 연속된 불릿/번호 라인. 한 블록 안에서 첫 라인의 종류(불릿/번호)가 태그를 정한다.
    // 줄머리 마커는 <li>가 대신하므로 리터럴 `*`/`-`가 본문에 새지 않는다(웹셀 날씨 등).
    // 부모 항목보다 깊게 들여쓴 연속 리스트 라인은 그 <li> 안의 **한 단계 중첩 목록**으로
    // 담는다 — 모델의 상품 관용("1. 제목" 밑에 들여쓴 "* 가격/평점")이 이전엔 종류가 갈리는
    // 지점에서 블록이 끊겨 납작한 형제 목록들로 조각났다(2026-07-28 실측: 경제 베스트 답).
    const bm = line.match(MD_BULLET_RE);
    const om = bm ? null : line.match(MD_ORDERED_RE);
    if (bm || om) {
      flush();
      const indentOf = (l) => l.match(/^\s*/)[0].length;
      const ordered = !!om;
      const list = document.createElement(ordered ? "ol" : "ul");
      list.className = "md-list";
      if (ordered && om[1] !== "1") {
        // 모델이 번호 항목 사이에 빈 줄을 쓰거나 스트리밍이 항목 사이에서 블록을 나누면
        // 목록이 여러 <ol>로 갈라진다. 원문 번호를 start로 보존해야 "1. 1."로 되감기지
        // 않는다(2026-07-23 실측 — CommonMark도 첫 항목 번호로 시작 번호를 정한다).
        list.setAttribute("start", om[1]);
      }
      while (i < lines.length) {
        const im = lines[i].match(ordered ? MD_ORDERED_RE : MD_BULLET_RE);
        if (!im) break;
        const parentIndent = indentOf(lines[i]);
        const li = document.createElement("li");
        renderInlineInto(li, ordered ? im[2] : im[1], o);
        // 한 단계 중첩: 다음 라인들이 (종류 무관) 리스트이고 부모보다 깊게 들여쓴 동안
        // li 안의 하위 목록으로 흡수한다. 종류가 바뀌면 하위 목록만 새로 연다.
        let sub = null;
        let subOrdered = null;
        while (i + 1 < lines.length) {
          const next = lines[i + 1];
          const nb = next.match(MD_BULLET_RE);
          const no = nb ? null : next.match(MD_ORDERED_RE);
          if (!(nb || no) || indentOf(next) <= parentIndent) break;
          const childOrdered = !!no;
          if (!sub || subOrdered !== childOrdered) {
            sub = document.createElement(childOrdered ? "ol" : "ul");
            sub.className = "md-list";
            li.appendChild(sub);
            subOrdered = childOrdered;
          }
          const childLi = document.createElement("li");
          renderInlineInto(childLi, childOrdered ? no[2] : nb[1], o);
          sub.appendChild(childLi);
          i++;
        }
        list.appendChild(li);
        i++;
      }
      i--;
      container.appendChild(list);
      continue;
    }
    buf.push(line);
  }
  flush();
}
