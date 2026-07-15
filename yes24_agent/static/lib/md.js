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
const MD_ORDERED_RE = /^\s*\d+[.)]\s+(.*)$/;

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

function makeMarker(sid, onMarker) {
  const badge = document.createElement("sup");
  badge.className = "marker";
  badge.textContent = sid;
  badge.tabIndex = 0;
  badge.setAttribute("role", "button");
  badge.setAttribute("aria-label", "출처 " + sid + " 보기");
  const fire = (e) => { e.stopPropagation(); if (onMarker) onMarker(sid); };
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
    ids.forEach((sid) => target.appendChild(makeMarker(sid, opts.onMarker)));
    last = re.lastIndex;
    afterMarker = true;
  }
  appendTextSlice(target, text.slice(last), afterMarker, false);
}

// 인라인: **볼드**를 분리하고 각 구간(볼드 포함) 안에서 마커를 조립한다 — 볼드 안 마커도 정상.
function renderInlineInto(target, text, opts) {
  const re = new RegExp(BOLD_RE.source, "g");
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) renderMarkersInto(target, text.slice(last, m.index), opts);
    const strong = document.createElement("strong");
    renderMarkersInto(strong, m[1], opts);
    target.appendChild(strong);
    last = re.lastIndex;
  }
  if (last < text.length) renderMarkersInto(target, text.slice(last), opts);
}

function splitCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
}

/**
 * 본문을 container에 렌더한다(기존 내용은 지운다).
 * opts.isCitation(id) → 그 id가 이번 턴 출처인가(배지 승격 조건). 기본: 승격 없음.
 * opts.onMarker(id)   → 배지 클릭·Enter 시 호출.
 */
export function renderBody(container, text, opts = {}) {
  const o = {
    isCitation: opts.isCitation || (() => false),
    onMarker: opts.onMarker || null,
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
          renderInlineInto(th, c, o);
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
          renderInlineInto(td, c, o);
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
    const bm = line.match(MD_BULLET_RE);
    const om = bm ? null : line.match(MD_ORDERED_RE);
    if (bm || om) {
      flush();
      const ordered = !!om;
      const list = document.createElement(ordered ? "ol" : "ul");
      list.className = "md-list";
      while (i < lines.length) {
        const im = lines[i].match(ordered ? MD_ORDERED_RE : MD_BULLET_RE);
        if (!im) break;
        const li = document.createElement("li");
        renderInlineInto(li, im[1], o);
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
