// 관리 화면 — 행 편집 패널(초기 질문), 본인 비밀번호 변경.
// 역할별로 컨트롤을 숨기는 것은 편의이고 판정은 서버다(숨긴 API를 직접 부르면 403).
// DB 값은 textContent·value로만 넣는다(마크업 삽입 금지). 인라인 style은 CSP가 막으므로 클래스만 쓴다.

/** 입력 종류별 읽기·쓰기·행 값 정규화 — 폼 값과 행 값을 같은 모양으로 비교해 바뀐 필드만 보낸다. */
const KINDS = {
  checkbox: { make: () => inputOf('checkbox'), set: (node, v) => { node.checked = Boolean(v); }, get: (node) => node.checked, norm: (v) => Boolean(v) },
  date: { make: () => inputOf('date'), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value || null, norm: (v) => v || null },
  text: { make: () => inputOf('text'), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value, norm: (v) => v ?? '' },
  password: { make: () => Object.assign(inputOf('password'), { autocomplete: 'new-password' }), set: (node) => { node.value = ''; }, get: (node) => node.value },
  textarea: { make: () => document.createElement('textarea'), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value, norm: (v) => v ?? '' },
  // 선택지는 field.options = [[값, 글자], …] — 초기 질문 슬롯(서버가 준 슬롯 키와 칩 라벨).
  select: { make: (field) => selectOf(field.options), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value, norm: (v) => v ?? '' },
};

function selectOf(options) {
  const node = document.createElement('select');
  node.append(...options.map(([value, text]) => new Option(text, value)));
  return node;
}

function inputOf(type) {
  const node = document.createElement('input');
  node.type = type;
  return node;
}

/** 역할 표시 이름 — 내부 값(viewer·editor·owner, 서버 /me roles)은 그대로 두고 화면 글자만 여기서 바꾼다(한 곳). */
export const ROLE_LABELS = Object.freeze({ viewer: '뷰어', editor: '운영자', owner: '관리자' });
export const roleLabel = (role) => ROLE_LABELS[role] ?? role;

const jsonBody = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
/** 편집 요청 한 벌 — 초기 질문 PATCH의 {changes, expected} 본문. */
const editBody = (changes, expected) => jsonBody('PATCH', { changes, expected });

export function initManage({ api, el, table, valueText, state, openDialog, showRecord, reload, onCreated, slotLabel, generateStatus, errorText, chanceText, slotProbability: slotChance, todayPreview, pins }) {
  const $ = (id) => document.getElementById(id);
  // 역할 순서는 서버 정본(/me의 roles, 낮은 권한 → 높은 권한)을 쓴다.
  const can = (role) => state.roles.indexOf(state.role) >= state.roles.indexOf(role);
  const actions = () => $('record-actions');

  /** fields: [{key, label, kind}]. values로 채운 폼과 상태 줄을 만든다.
   *  form.dirty()는 마지막으로 채운 값과 다른지 — 옆 패널을 떠날 때 확인에 쓴다(admin.js panelDirty). */
  function buildForm(title, fields, values, submitLabel) {
    const form = el('form', 'manage-form');
    const nodes = {};
    form.append(el('h3', null, title));
    for (const field of fields) {
      nodes[field.key] = KINDS[field.kind].make(field);
      const label = el('label', field.kind === 'checkbox' ? 'check' : null, field.label);
      // 체크박스는 상자를 앞에(라벨 전체가 누르는 자리), 나머지 칸은 라벨 아래.
      if (field.kind === 'checkbox') label.prepend(nodes[field.key]); else label.append(nodes[field.key]);
      form.append(label);
      if (field.help) form.append(el('small', 'field-help', field.help));  // 개념 설명 한 줄(체크박스 바로 아래)
    }
    const button = el('button', 'primary', submitLabel);
    button.type = 'submit';
    const status = el('p', 'analysis-note', '');
    status.setAttribute('role', 'status');
    form.append(button, status);
    const read = () => Object.fromEntries(fields.map((field) => [field.key, KINDS[field.kind].get(nodes[field.key])]));
    let filled = '';
    const fill = (next) => { for (const field of fields) KINDS[field.kind].set(nodes[field.key], next[field.key]); filled = JSON.stringify(read()); };
    form.dirty = () => JSON.stringify(read()) !== filled;
    fill(values);
    // 제출 시도마다 이전 결과 문구를 지운다 — 브라우저 검증이 제출을 막는 경우(invalid)도 포함.
    form.addEventListener('invalid', () => { status.textContent = ''; }, true);
    let recover = () => '';
    /** submit(read 결과) → 성공 문구. 실패는 서버 detail을 그대로 보인다. */
    const onSubmit = (submit) => {
      form.onsubmit = async (event) => {
        event.preventDefault();
        button.disabled = true;
        status.textContent = '처리하는 중…';
        try { status.textContent = await submit(read()); }
        catch (error) { if (error.name !== 'AbortError') status.textContent = recover(error) || errorText(error, '저장하지 못했습니다. 다시 시도해 주세요.'); }
        finally { button.disabled = false; }
      };
    };
    return { form, nodes, fill, status, onSubmit, onConflict: (handler) => { recover = handler; } };
  }

  /**
   * 편집 프로토콜(expected 동봉): 바뀐 필드만 changes로, 그 필드의 행 값을 expected로 보낸다.
   * 409에 current가 오면 행과 폼을 최신 값으로 갱신하고 서버 문구 뒤에 안내를 붙인다.
   */
  function editForm({ title, fields, row, path, after }) {
    const built = buildForm(title, fields, row, '저장');
    built.onSubmit(async (values) => {
      const changes = {}, expected = {};
      for (const field of fields) {
        const before = KINDS[field.kind].norm(row[field.key]);
        if (values[field.key] !== before) { changes[field.key] = values[field.key]; expected[field.key] = before; }
      }
      if (!Object.keys(changes).length) return '바뀐 값이 없습니다.';
      const result = await api(path, editBody(changes, expected));
      Object.assign(row, changes);
      built.fill(row);
      showRecord(row);
      reload();
      return `저장했습니다.${after ? after(changes, expected, result) : ''}`;
    });
    built.onConflict((error) => {
      if (error.status !== 409 || !error.body?.current) return '';
      Object.assign(row, error.body.current);
      built.fill(row);
      showRecord(row);
      return `${error.message} 최신 값으로 폼을 갱신했습니다. 확인 후 다시 저장하세요.`;
    });
    return built;
  }

  // ── 초기 질문 행 패널 ────────────────────────────────────────────────────
  // 고정은 세 갈래 선택(PINS 이름 — '' · slot · global). 선택 아래 한 줄은 고른 종류의 설명이다.
  const STARTER_FIELDS = [
    { key: 'text', label: '문장', kind: 'textarea' },
    { key: 'pinned', label: '고정', kind: 'select', options: Object.entries(pins).map(([kind, { label }]) => [kind, label]), help: ' ' },
    { key: 'active', label: '노출 켜기', kind: 'checkbox' },
    { key: 'valid_from', label: '노출 시작일', kind: 'date' },
    { key: 'valid_until', label: '노출 종료일', kind: 'date', help: '시작일·종료일을 비워 두면 매일 노출돼요.' },
  ];

  // 고정의 영향 — 행 패널·직접 등록 폼(고르는 순간)과 날짜 패널(고정 확인 줄)이 같은 말을 쓴다. data = 그날
  // 미리보기(rows·slots·global_pins). kind = 거는 고정('slot' · 'global').
  // 분야 고정: 같은 분야에 분야 고정이 이미 있으면 서빙은 그 질문들끼리 무작위로 고른다(교체가 아니다).
  // 첫 화면 고정: 매번 먼저 들어가므로 100%, 그 분야의 다른 질문은 첫 화면에 안 나온다(분야 칸을 쓴다).
  function pinImpact(data, slot, id, live = true, kind = 'slot') {
    // 지금 노출 중이 아닌 행(목록 판정 live=false, 또는 그날 풀에 없음)은 고정해도 첫 화면에 나오지 않는다 — 수치를 보이지 않는다.
    if (!live || (id != null && !data?.rows?.some((row) => row.id === id))) return '지금 노출 중이 아니라 고정해도 첫 화면에 나오지 않아요.';
    const siblings = (data?.rows ?? []).filter((row) => row.slot === slot && row.id !== id);
    const current = data?.rows?.find((row) => row.id === id)?.probability;
    const from = current == null ? '예상 ' : current === 0 ? '지금 안 나옴 → ' : `${chanceText(current)} → `;
    if (kind === 'global') {
      // 결과 먼저 — 이 분야 칸은 이 질문이 쓴다. 사용 수는 서버 등록 검사(422)와 같은 셈(global_pins.count).
      const { count = 0, limit = 0 } = data?.global_pins ?? {}, already = data?.rows?.find((row) => row.id === id)?.pinned === 'global';
      const sameSlot = siblings.find((row) => row.pinned === 'global');
      if (sameSlot) return `이 분야엔 이미 첫 화면 고정 "${sameSlot.text}"이 있어요 — 분야당 하나까지라 저장할 수 없어요. 아래에서 풀고 다시 고르세요.`;
      const after = count + (already ? 0 : 1);
      if (after > limit) return `첫 화면 고정은 최대 ${limit}개예요(지금 ${count}개) — 아래에서 하나를 풀면 저장할 수 있어요.`;
      // 다른 분야 — 첫 화면 고정이 칸 하나·분야 하나를 더 쓰면 남은 칸 ÷ 남은 분야가 바뀐다(pick_probabilities와 같은 식).
      const pins = data?.global_pins ?? { used: 0, slots: 0 }, slotTaken = (data?.rows ?? []).some((row) => row.slot === slot && row.pinned === 'global');
      const others = already || slotTaken ? '' : `다른 분야가 뽑힐 확률 ${chanceText(slotChance(data) ?? 0)} → ${chanceText(slotChance({ ...data, global_pins: { ...pins, used: (pins.used ?? 0) + 1, slots: (pins.slots ?? 0) + 1 } }) ?? 0)}.`;
      return [`이 분야는 이 질문으로 고정돼요${siblings.length ? ` — 같은 분야의 다른 질문 ${siblings.length}개는 나오지 않아요` : ''}.`,
        `이 질문 ${from}100% · 첫 화면 고정 ${after}/${limit}개.`, others].filter(Boolean).join(' ');
    }
    // 그 분야에 첫 화면 고정이 있으면 분야 칸은 그 질문이 쓴다 — 분야 고정은 효과가 없다.
    const global = siblings.find((row) => row.pinned === 'global');
    if (global) return `이 분야엔 첫 화면 고정 "${global.text}"이 있어 분야 고정은 효과가 없어요(그 질문이 이 분야 칸을 씁니다).`;
    const pinned = siblings.filter((row) => row.pinned === 'slot');
    // 머리말과 같은 값(남은 칸 ÷ 남은 분야) — 풀에 없는 새 분야면 분야가 하나 늘어난 분모로.
    const slotProbability = slotChance(data, data?.slots?.some((item) => item.slot === slot) ? 0 : 1);
    const change = slotProbability == null ? '' : `이 질문 ${from}${chanceText(slotProbability / (pinned.length + 1))}${pinned.length ? '(분야 고정 질문끼리 나눠 가져요)' : ''}.`;
    const effect = pinned.length ? `이미 분야 고정된 "${pinned[0].text}"${pinned.length > 1 ? ` 외 ${pinned.length - 1}개` : ''}와 번갈아 나와요(교체되지 않아요).`
      : siblings.length ? `같은 분야의 다른 질문 ${siblings.length}개는 첫 화면에서 가려져요.` : '';
    return [change, effect].filter(Boolean).join(' ');
  }
  /** 고정을 막는 첫 화면 고정 목록 — 같은 분야의 첫 화면 고정이나(분야당 하나) 상한이 찼을 때의 지금 목록.
   *  날짜 패널 확인 줄이 확인 버튼을 끄고 각각 '해제'를 붙인다. 막히지 않으면 null. */
  function pinBlockers(data, slot, id, kind) {
    if (kind !== 'global') return null;
    const { count = 0, limit = 0, items = [] } = data?.global_pins ?? {};
    const sameSlot = (data?.rows ?? []).find((row) => row.slot === slot && row.id !== id && row.pinned === 'global');
    if (sameSlot) return items.filter((item) => item.id === sameSlot.id);
    const already = data?.rows?.find((row) => row.id === id)?.pinned === 'global';
    return count + (already ? 0 : 1) > limit ? items : null;
  }
  /** 고정 종류 설명 한 줄 — 분야 대표는 지금 분야 확률, 첫 화면 고정은 상한을 붙인다(같은 함수 값). */
  const pinHelp = (kind) => (kind === 'global' ? `${pins.global.help}(최대 ${state.preview?.global_pins?.limit ?? '-'}개).`
    : kind === 'slot' ? `${pins.slot.help}(지금 분야가 뽑힐 확률 ${chanceText(slotChance(state.preview) ?? 0)}).` : '');
  const pinNotice = (data, slot, id, kind) => `${pinHelp(kind)} ${pinImpact(data, slot, id, true, kind)}`.trim();
  /** 고정 선택 — 고른 종류의 설명(도움말 줄)과 영향은 고정을 골랐을 때만 보인다. 영향은 오늘 미리보기(state.preview).
   *  slot()·id = 지금 폼의 분야 키와 자기 행 id(새 질문은 null). */
  function pinWarning(built, slot, id, live) {
    const line = el('small', 'field-warn'), help = built.nodes.pinned.closest('label').nextElementSibling;
    line.setAttribute('role', 'status');
    help.after(line);  // 도움말 줄 다음
    const update = () => {
      const kind = built.nodes.pinned.value;
      help.hidden = !kind;
      help.textContent = pinHelp(kind);
      line.textContent = kind ? pinImpact(state.preview, slot(), id, live, kind) : '';
    };
    built.nodes.pinned.addEventListener('change', update);
    update();
    return update;
  }

  function starterPanel(row) {
    if (!can('editor')) return;
    const built = editForm({
      title: '초기 질문 편집', fields: STARTER_FIELDS, row,
      path: `/admin/starters/${encodeURIComponent(row.id)}`,
    });
    pinWarning(built, () => row.slot, row.id, row.live);
    actions().append(built.form);
  }

  // 슬롯 선택지 = 풀에 있는 슬롯(목록 응답 slots — 키는 값으로만, 운영자에겐 칩 라벨).
  const slotOptions = () => state.starterSlots.map(({ slot, label }) => [slot, label]);

  // 새 분야는 이름(첫 화면 칩에 그대로 보이는 글자) 하나만 받는다 — 이름이 곧 분야 키다(키가 라벨이 되는
  // 규칙은 starters.chip_label). 이미 있는 분야 이름을 치면 새로 만들지 않고 그 분야로 등록한다.
  const NEW_SLOT = '\u0000new';
  /** prefill.text가 있으면(인기 질문의 '칩으로 추가') 문장을 채워 열고 개인정보 확인을 안내한다.
   *  estimate가 있으면(캘린더 기간 추가) 슬롯·고정·시작일을 바꿀 때마다 등록 전 예상 노출 확률을 보인다. */
  async function openStarterCreate(prefill = {}, { estimate } = {}) {
    if (!(await openDialog('첫 화면 질문 직접 등록'))) return;
    // 분야 선택지는 지금 노출 중인 분야가 먼저(오늘 미리보기의 분야), 나머지는 '(지금 노출 없음)'을 붙여 뒤에.
    await todayPreview();
    const live = new Set((state.preview?.slots ?? []).map((item) => item.slot));
    const options = [...slotOptions().filter(([key]) => live.has(key)), ...slotOptions().filter(([key]) => !live.has(key)).map(([key, label]) => [key, `${label} (지금 노출 없음)`])];
    const built = buildForm('새 초기 질문', [
      { key: 'slot', label: '분야', kind: 'select', options: [['', '분야 선택'], ...options, [NEW_SLOT, '+ 새 분야 만들기']] },
      { key: 'new_slot', label: '새 분야 이름', kind: 'text', help: '첫 화면 질문 위에 그대로 보이는 이름이에요(예: 가을 이벤트).' },
      ...STARTER_FIELDS.filter((field) => field.key !== 'active'),
    ], prefill, '추가');
    if (prefill.text) built.form.querySelector('h3').after(el('p', 'analysis-note notice', '사용자가 직접 입력한 문장입니다 — 이름·연락처·주문 정보 같은 개인정보가 없는지 확인하고, 첫 화면 질문에 맞게 다듬어 등록하세요.'));
    const { slot, new_slot: typed } = built.nodes;
    // 새 분야 이름은 칩 라벨이 된다 — 서버와 같은 상한(미리보기 응답 slot_name_max), ':'는 서버가 거른다.
    if (state.preview?.slot_name_max) typed.maxLength = state.preview.slot_name_max;
    slot.required = true;
    const syncNew = () => {
      const label = typed.closest('label'), shown = slot.value === NEW_SLOT;
      label.hidden = label.nextElementSibling.hidden = !shown;  // 칸과 그 도움말 줄
      typed.required = shown;
    };
    slot.addEventListener('change', syncNew);
    syncNew();
    const slotKey = () => (slot.value === NEW_SLOT ? state.starterSlots.find((item) => item.label === typed.value.trim())?.slot ?? typed.value.trim() : slot.value);
    const warn = pinWarning(built, slotKey, null);
    slot.addEventListener('change', warn);
    // 이 분야의 지금 질문 — 같은 뜻의 질문이 이미 있는지 운영자가 직접 본다(문구 비교로 판정하지 않는다). 접어 둔다.
    const current = el('details', 'slot-current');
    slot.closest('label').after(current);
    const listCurrent = () => {
      const rows = (state.preview?.rows ?? []).filter((row) => row.slot === slotKey());
      current.hidden = !slotKey();
      const summary = el('summary', null, `이 분야의 지금 질문 ${rows.length}개`), list = el('ul');
      list.append(...rows.map((row) => el('li', null, row.text)));
      current.replaceChildren(summary, ...(rows.length ? [list] : [el('p', null, '지금 노출 중인 질문이 없어요.')]));
    };
    for (const node of [slot, typed]) node.addEventListener('change', listCurrent);
    listCurrent();
    if (estimate) {
      const line = el('p', 'analysis-note', '분야를 고르면 첫 화면에 뜰 확률(예상치)을 보입니다.');
      line.setAttribute('role', 'status');
      built.form.querySelector('button.primary').before(line);
      let asked = 0;
      const update = async () => {
        const key = slotKey();
        if (!key) { line.textContent = '분야를 고르면 첫 화면에 뜰 확률(예상치)을 보입니다.'; return; }
        const ticket = ++asked;
        try {
          const result = await estimate({ slot: key, pinned: built.nodes.pinned.value, date: built.nodes.valid_from.value });
          if (ticket === asked) line.textContent = `첫 화면에 뜰 확률 ${chanceText(result.probability)} (${result.date} 기준 · 등록 전 예상치)`;
        } catch (error) { if (ticket === asked) line.textContent = `예상 확률을 계산하지 못했습니다: ${errorText(error, '다시 시도해 주세요.')}`; }
      };
      for (const node of [slot, typed, built.nodes.pinned, built.nodes.valid_from]) node.addEventListener('change', update);
    }
    built.onSubmit(async ({ new_slot: _typed, ...values }) => {  // 새 분야 이름은 slotKey()가 읽는다
      values.slot = slotKey();
      const created = await api('/admin/starters', jsonBody('POST', values));
      built.fill({});  // 기준값을 비워 두어 패널을 닫을 때 묻지 않는다
      onCreated(created);  // 화면이 패널을 닫고 알림·새 행 강조를 맡는다
      return '추가했습니다.';
    });
    actions().append(built.form);
  }

  async function openStarterGenerate() {
    if (!(await openDialog('초기 질문 지금 생성'))) return;
    const built = buildForm('즉시 생성', [{ key: 'slot', label: '분야', kind: 'select', options: [['', '자동 생성 분야 전체'], ...slotOptions()] }, { key: 'force', label: '오늘 실행분이 있어도 다시 생성', kind: 'checkbox' }], {}, '생성 실행');
    built.onSubmit(async (values) => {
      const params = new URLSearchParams({ force: values.force ? '1' : '0' });
      if (values.slot) params.set('slot', values.slot);
      built.status.textContent = '생성하는 중… 수십 초 걸릴 수 있습니다.';
      const result = await api(`/admin/starters/generate?${params}`, { method: 'POST' });
      // 결과는 분야 키별 {status, inserted} — 분야 이름 · 결과 한 줄로(키·JSON을 그대로 보이지 않는다).
      showRecord(Object.fromEntries(Object.entries(result).map(([key, item]) => [slotLabel(key), `${generateStatus[item.status] ?? item.status} · 새 질문 ${item.inserted ?? 0}개${item.status === 'failed' && item.detail ? ` — ${item.detail}` : ''}`])), null);
      built.fill(values);  // 실행한 값이 새 기준 — 패널을 떠나도 묻지 않는다
      reload();  // 지금 화면(초기 질문)의 목록을 다시 읽는다
      return '생성을 마쳤습니다. 분야별 결과는 위 표입니다.';
    });
    actions().append(built.form);
  }

  function starterTools(tools) {
    tools.replaceChildren();
    if (!can('editor')) return;
    const add = el('button', 'crema-button', '직접 등록');
    add.onclick = () => openStarterCreate();  // 클릭 이벤트가 채울 값(prefill)으로 넘어가지 않게
    const generate = el('button', 'crema-button', '지금 생성');
    generate.onclick = () => openStarterGenerate();
    tools.append(add, generate);
  }

  // ── 본인 비밀번호 ──────────────────────────────────────────────────────
  /** 새 비밀번호는 확인 칸과 같아야 하고 최소 길이(서버 설정 /me password_min_length)를 안내·사전 검사한다 — 판정은 서버. */
  async function openPasswordChange() {
    if (!(await openDialog('비밀번호 변경'))) return;
    const min = state.passwordMin;
    const built = buildForm('내 비밀번호', [
      { key: 'current_password', label: '현재 비밀번호', kind: 'password' },
      { key: 'new_password', label: `새 비밀번호 (${min}자 이상)`, kind: 'password' },
      { key: 'confirm_password', label: '새 비밀번호 확인', kind: 'password' },
    ], {}, '변경');
    built.nodes.current_password.autocomplete = 'current-password';
    for (const node of Object.values(built.nodes)) node.required = true;
    built.nodes.new_password.minLength = built.nodes.confirm_password.minLength = min;
    // 입력하는 동안 길이·일치를 바로 알린다(제출 전 — 판정은 여전히 서버).
    const live = el('p', 'analysis-note password-live');
    live.setAttribute('aria-live', 'polite');
    built.form.querySelector('button.primary').before(live);
    const check = () => {
      const next = built.nodes.new_password.value, again = built.nodes.confirm_password.value;
      if (!next && !again) { live.textContent = ''; return; }
      const length = next.length >= min ? `길이 ${next.length}자 ✓` : `길이 ${next.length}/${min}자`;
      live.textContent = again ? `${length} · ${again === next ? '두 칸이 같습니다 ✓' : '두 칸이 다릅니다'}` : length;
      live.classList.toggle('ok', next.length >= min && again === next);
    };
    for (const node of [built.nodes.new_password, built.nodes.confirm_password]) node.addEventListener('input', check);
    built.onSubmit(async ({ confirm_password: confirmed, ...values }) => {
      if (confirmed !== values.new_password) throw new Error('새 비밀번호와 확인이 다릅니다.');
      await api('/admin/api/me/password', jsonBody('POST', values));
      built.fill({});
      return '변경했습니다. 이 세션을 제외한 다른 세션은 로그아웃됐습니다.';
    });
    actions().append(built.form);
  }

  return { starterPanel, starterTools, openStarterCreate, openPasswordChange, pinNotice, pinBlockers };
}
