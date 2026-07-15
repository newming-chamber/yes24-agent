// RBTI 축 정의 — persona.py AXIS_ORDER와 동형인 **단일 출처**.
// 백엔드 계약: 코드 = [pattern][processing][breadth][motivation] = [C/S][A/E][D/B][I/F].
// **배열 순서가 곧 코드 자릿수 순서다.** 이전엔 채팅(RBTI_AXES)과 매트릭스(AXES+VAL)가 이
// 계약을 각자 들고 있어(이원화) 한쪽만 바뀌면 조용히 어긋났다 — 여기 하나만 고치면 된다.
//
// short : 매트릭스 필터 칩·헤더용 짧은 라벨
// title/desc : 채팅 페르소나 팝오버용 설명

export const AXES = [
  { key: "pattern", label: "독서 패턴", shortLabel: "패턴", options: [
    { code: "C", short: "완독", title: "정독·완독", desc: "한 권을 끝까지" },
    { code: "S", short: "선택", title: "발췌·탐색", desc: "여러 권 골라 읽기" } ] },
  { key: "processing", label: "정보 처리", shortLabel: "처리", options: [
    { code: "A", short: "분석", title: "논리·분석", desc: "근거와 구조로" },
    { code: "E", short: "공감", title: "감성·공감", desc: "감정과 울림으로" } ] },
  { key: "breadth", label: "취향의 폭", shortLabel: "폭", options: [
    { code: "D", short: "깊이", title: "깊이·심화", desc: "한 분야 깊게" },
    { code: "B", short: "넓이", title: "넓이·확장", desc: "여러 분야 넓게" } ] },
  { key: "motivation", label: "독서 동기", shortLabel: "동기", options: [
    { code: "I", short: "정보", title: "지식·정보", desc: "배우고 얻으려" },
    { code: "F", short: "재미", title: "재미·즐거움", desc: "즐기고 몰입하려" } ] },
];

// 코드 글자 → 짧은 라벨(C:"완독" …). 축 정의에서 파생 — 별도 표를 두지 않는다.
export const VAL = Object.fromEntries(
  AXES.flatMap((a) => a.options.map((o) => [o.code, o.short]))
);

// 축별 코드 배열(파생) — 별도 표를 두지 않는다. 매트릭스 필터·토너먼트가 이 형태를 쓴다.
for (const a of AXES) a.values = a.options.map((o) => o.code);
const values = (i) => AXES[i].values;

// 16 코드 = 축값 데카르트 곱(matrix_codes()와 동일 순서 → col 인덱스와 일치).
export const CODES = [];
for (const p of values(0))
  for (const pr of values(1))
    for (const b of values(2))
      for (const m of values(3)) CODES.push(p + pr + b + m);

// 4×4: 행 = 패턴×처리(code[0..1]), 열 = 폭×동기(code[2..3]).
export const ROW_ORDER = [];
for (const p of values(0)) for (const pr of values(1)) ROW_ORDER.push(p + pr);
export const COL_ORDER = [];
for (const b of values(2)) for (const m of values(3)) COL_ORDER.push(b + m);

export function rowKey(code) { return code[0] + code[1]; }
export function colKey(code) { return code[2] + code[3]; }
export function deriveAxisLabel(code) {
  return [...code].map((ch) => VAL[ch]).join("-");
}

// 코드 유효성: 4글자 + 축별 허용값. 무효면 null(페르소나 미적용).
export function parseCode(code) {
  if (typeof code !== "string" || code.length !== AXES.length) return null;
  const sel = {};
  for (let i = 0; i < AXES.length; i++) {
    if (!values(i).includes(code[i])) return null;
    sel[AXES[i].key] = code[i];
  }
  return sel;
}

// 축별 선택 → 코드(4축 모두 선택됐을 때만), 아니면 null.
export function deriveCode(sel) {
  const parts = AXES.map((a) => sel[a.key]);
  return parts.every(Boolean) ? parts.join("") : null;
}

// 두 페이지가 공유하는 저장 키(채택 페르소나·매트릭스→채팅 핸드오프).
export const RBTI_STORAGE_KEY = "yes24_rbti";
export const MATRIX_HANDOFF_KEY = "yes24_matrix_handoff";
