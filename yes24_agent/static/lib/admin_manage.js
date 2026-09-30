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

export function initManage({ api, el, table, valueText, state, openDialog, showRecord, reload }) {
  const $ = (id) => document.getElementById(id);
  // 역할 순서는 서버 정본(/me의 roles, 낮은 권한 → 높은 권한)을 쓴다.
  const can = (role) => state.roles.indexOf(state.role) >= state.roles.indexOf(role);
  const actions = () => $('record-actions');

  /** fields: [{key, label, kind}]. values로 채운 폼과 상태 줄을 만든다.
   *  form.dirty()는 마지막으로 채운 값과 다른지 — 옆 패널을 떠날 때 확인에 쓴다(admin.js canLeavePanel). */
  function buildForm(title, fields, values, submitLabel) {
    const form = el('form', 'manage-form');
    const nodes = {};
    form.append(el('h3', null, title));
    for (const field of fields) {
      nodes[field.key] = KINDS[field.kind].make(field);
      const label = el('label', field.kind === 'checkbox' ? 'check' : null, field.label);
      label.append(nodes[field.key]);
      form.append(label);
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
        catch (error) { if (error.name !== 'AbortError') status.textContent = recover(error) || error.message; }
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
  const STARTER_FIELDS = [
    { key: 'text', label: '문장', kind: 'textarea' },
    { key: 'pinned', label: '고정', kind: 'checkbox' },
    { key: 'active', label: '노출', kind: 'checkbox' },
    { key: 'valid_from', label: '노출 시작일', kind: 'date' },
    { key: 'valid_until', label: '노출 종료일', kind: 'date' },
  ];

  function starterPanel(row) {
    if (!can('editor')) return;
    actions().append(editForm({
      title: '초기 질문 편집', fields: STARTER_FIELDS, row,
      path: `/admin/starters/${encodeURIComponent(row.id)}`,
    }).form);
  }

  // 슬롯 선택지 = 풀에 있는 슬롯(목록 응답 slots — 키는 값으로만, 운영자에겐 칩 라벨).
  const slotOptions = () => state.starterSlots.map(({ slot, label }) => [slot, label]);

  // 수동 추가는 기존 슬롯 외에 새 슬롯 키도 받는다(형식 판정은 서버 검증 그대로).
  const NEW_SLOT = '\u0000new';
  function openStarterCreate() {
    if (!openDialog('초기 질문 수동 추가')) return;
    const built = buildForm('새 초기 질문', [
      { key: 'slot', label: '슬롯', kind: 'select', options: [['', '슬롯 선택'], ...slotOptions(), [NEW_SLOT, '새 슬롯 직접 입력']] },
      { key: 'new_slot', label: '새 슬롯 키', kind: 'text' },
      ...STARTER_FIELDS.filter((field) => field.key !== 'active'),
    ], {}, '추가');
    const { slot, new_slot: typed } = built.nodes;
    slot.required = true;
    const syncNew = () => { typed.closest('label').hidden = slot.value !== NEW_SLOT; typed.required = slot.value === NEW_SLOT; };
    slot.addEventListener('change', syncNew);
    syncNew();
    built.onSubmit(async ({ new_slot: newSlot, ...values }) => {
      if (values.slot === NEW_SLOT) values.slot = newSlot.trim();
      const created = await api('/admin/starters', jsonBody('POST', values));
      reload();
      built.fill({});
      syncNew();
      return `추가했습니다 (id ${created.id}).`;
    });
    actions().append(built.form);
  }

  function openStarterGenerate() {
    if (!openDialog('초기 질문 지금 생성')) return;
    const built = buildForm('즉시 생성', [{ key: 'slot', label: '슬롯', kind: 'select', options: [['', '전체 자동 슬롯'], ...slotOptions()] }, { key: 'force', label: '오늘 실행분이 있어도 다시 생성', kind: 'checkbox' }], {}, '생성 실행');
    built.onSubmit(async (values) => {
      const params = new URLSearchParams({ force: values.force ? '1' : '0' });
      if (values.slot) params.set('slot', values.slot);
      built.status.textContent = '생성하는 중… 수십 초 걸릴 수 있습니다.';
      const result = await api(`/admin/starters/generate?${params}`, { method: 'POST' });
      showRecord(result, null);
      built.fill(values);  // 실행한 값이 새 기준 — 패널을 떠나도 묻지 않는다
      reload();  // 지금 화면(초기 질문)의 목록을 다시 읽는다
      return '생성을 마쳤습니다. 슬롯별 결과는 위 표입니다.';
    });
    actions().append(built.form);
  }

  function starterTools(tools) {
    tools.replaceChildren();
    if (!can('editor')) return;
    const add = el('button', 'crema-button', '수동 추가');
    add.onclick = openStarterCreate;
    const generate = el('button', 'crema-button', '지금 생성');
    generate.onclick = openStarterGenerate;
    tools.append(add, generate);
  }

  // ── 본인 비밀번호 ──────────────────────────────────────────────────────
  /** 새 비밀번호는 확인 칸과 같아야 하고 최소 길이(서버 설정 /me password_min_length)를 안내·사전 검사한다 — 판정은 서버. */
  function openPasswordChange() {
    if (!openDialog('비밀번호 변경')) return;
    const min = state.passwordMin;
    const built = buildForm('내 비밀번호', [
      { key: 'current_password', label: '현재 비밀번호', kind: 'password' },
      { key: 'new_password', label: `새 비밀번호 (${min}자 이상)`, kind: 'password' },
      { key: 'confirm_password', label: '새 비밀번호 확인', kind: 'password' },
    ], {}, '변경');
    built.nodes.current_password.autocomplete = 'current-password';
    for (const node of Object.values(built.nodes)) node.required = true;
    built.nodes.new_password.minLength = built.nodes.confirm_password.minLength = min;
    built.onSubmit(async ({ confirm_password: confirmed, ...values }) => {
      if (confirmed !== values.new_password) throw new Error('새 비밀번호와 확인이 다릅니다.');
      await api('/admin/api/me/password', jsonBody('POST', values));
      built.fill({});
      return '변경했습니다. 이 세션을 제외한 다른 세션은 로그아웃됐습니다.';
    });
    actions().append(built.form);
  }

  return { starterPanel, starterTools, openPasswordChange };
}
