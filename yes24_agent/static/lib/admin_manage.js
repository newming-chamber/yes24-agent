// 관리 화면 — 행 편집 패널(회원 차단·초기 질문), 관리자 계정, 본인 비밀번호 변경.
// 역할별로 컨트롤을 숨기는 것은 편의이고 판정은 서버다(숨긴 API를 직접 부르면 403).
// DB 값은 textContent·value로만 넣는다(마크업 삽입 금지). 인라인 style은 CSP가 막으므로 클래스만 쓴다.

/** 입력 종류별 읽기·쓰기·행 값 정규화 — 폼 값과 행 값을 같은 모양으로 비교해 바뀐 필드만 보낸다. */
const KINDS = {
  checkbox: { make: () => inputOf('checkbox'), set: (node, v) => { node.checked = Boolean(v); }, get: (node) => node.checked, norm: (v) => Boolean(v) },
  date: { make: () => inputOf('date'), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value || null, norm: (v) => v || null },
  text: { make: () => inputOf('text'), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value, norm: (v) => v ?? '' },
  password: { make: () => Object.assign(inputOf('password'), { autocomplete: 'new-password' }), set: (node) => { node.value = ''; }, get: (node) => node.value },
  textarea: { make: () => document.createElement('textarea'), set: (node, v) => { node.value = v ?? ''; }, get: (node) => node.value, norm: (v) => v ?? '' },
};

function inputOf(type) {
  const node = document.createElement('input');
  node.type = type;
  return node;
}

const jsonBody = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
/** 편집 요청 한 벌 — 회원·초기 질문·관리자 PATCH 모두 {changes, expected} 본문이다. */
const editBody = (changes, expected) => jsonBody('PATCH', { changes, expected });

export function initManage({ api, el, table, clock, valueText, state, openDialog, showRecord, reload }) {
  const $ = (id) => document.getElementById(id);
  // 역할 순서는 서버 정본(/me의 roles, 낮은 권한 → 높은 권한)을 쓴다.
  const can = (role) => state.roles.indexOf(state.role) >= state.roles.indexOf(role);
  KINDS.role = { make: () => { const select = document.createElement('select'); select.append(...state.roles.map((role) => new Option(role, role))); return select; }, set: (node, v) => { node.value = v; }, get: (node) => node.value, norm: (v) => v };
  const actions = () => $('record-actions');
  const moment = (value) => (typeof value === 'number' ? clock(value) : valueText(value));

  /** fields: [{key, label, kind}]. values로 채운 폼과 상태 줄을 만든다. */
  function buildForm(title, fields, values, submitLabel) {
    const form = el('form', 'manage-form');
    const nodes = {};
    form.append(el('h3', null, title));
    for (const field of fields) {
      nodes[field.key] = KINDS[field.kind].make();
      const label = el('label', field.kind === 'checkbox' ? 'check' : null, field.label);
      // 선택 상자는 화살표를 그릴 span으로 감싼다(CSS 한 곳, data URI 없이).
      if (field.kind === 'role') { const wrap = el('span', 'select'); wrap.append(nodes[field.key]); label.append(wrap); } else label.append(nodes[field.key]);
      form.append(label);
    }
    const button = el('button', 'primary', submitLabel);
    button.type = 'submit';
    const status = el('p', 'analysis-note', '');
    status.setAttribute('role', 'status');
    form.append(button, status);
    const fill = (next) => { for (const field of fields) KINDS[field.kind].set(nodes[field.key], next[field.key]); };
    const read = () => Object.fromEntries(fields.map((field) => [field.key, KINDS[field.kind].get(nodes[field.key])]));
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

  // ── 회원 · 초기 질문 행 패널 ──────────────────────────────────────────
  const USER_FIELDS = [{ key: 'is_active', label: '이용 가능 — 끄면 다음 요청부터 대화가 막힙니다', kind: 'checkbox' }];
  const STARTER_FIELDS = [
    { key: 'text', label: '문장', kind: 'textarea' },
    { key: 'pinned', label: '고정', kind: 'checkbox' },
    { key: 'active', label: '활성', kind: 'checkbox' },
    { key: 'valid_from', label: '노출 시작일', kind: 'date' },
    { key: 'valid_until', label: '노출 종료일', kind: 'date' },
  ];

  function userPanel(row) {
    if (!can('editor')) return;
    actions().append(editForm({
      title: '회원 이용 가능 여부', fields: USER_FIELDS, row,
      path: `/admin/api/users/${encodeURIComponent(row.id)}`,
      after: (changes) => (changes.is_active === false ? ' 다음 요청부터 이 회원의 대화가 막힙니다.' : ''),
    }).form);
  }

  function starterPanel(row) {
    if (!can('editor')) return;
    actions().append(editForm({
      title: '초기 질문 편집', fields: STARTER_FIELDS, row,
      path: `/admin/starters/${encodeURIComponent(row.id)}`,
    }).form);
  }

  function openStarterCreate() {
    openDialog('초기 질문 수동 추가');
    const built = buildForm('새 초기 질문', [{ key: 'slot', label: '슬롯', kind: 'text' }, ...STARTER_FIELDS.filter((field) => field.key !== 'active')], {}, '추가');
    built.onSubmit(async (values) => {
      const created = await api('/admin/starters', jsonBody('POST', values));
      reload();
      built.fill({});
      return `추가했습니다 (id ${created.id}).`;
    });
    actions().append(built.form);
  }

  function openStarterGenerate() {
    openDialog('초기 질문 지금 생성');
    const built = buildForm('즉시 생성', [{ key: 'slot', label: '슬롯 (비우면 전체 자동 슬롯)', kind: 'text' }, { key: 'force', label: '오늘 실행분이 있어도 다시 생성', kind: 'checkbox' }], {}, '생성 실행');
    built.onSubmit(async (values) => {
      const params = new URLSearchParams({ force: values.force ? '1' : '0' });
      if (values.slot.trim()) params.set('slot', values.slot.trim());
      built.status.textContent = '생성하는 중… 수십 초 걸릴 수 있습니다.';
      const result = await api(`/admin/starters/generate?${params}`, { method: 'POST' });
      showRecord(result, null);
      reload();  // 지금 화면(회원·초기 질문·관리자)의 목록을 다시 읽는다
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
  function openPasswordChange() {
    openDialog('비밀번호 변경');
    const built = buildForm('내 비밀번호', [{ key: 'current_password', label: '현재 비밀번호', kind: 'password' }, { key: 'new_password', label: '새 비밀번호', kind: 'password' }], {}, '변경');
    built.nodes.current_password.autocomplete = 'current-password';
    built.onSubmit(async (values) => {
      await api('/admin/api/me/password', jsonBody('POST', values));
      built.fill({});
      return '변경했습니다. 이 세션을 제외한 다른 세션은 로그아웃됐습니다.';
    });
    actions().append(built.form);
  }

  // ── 관리자 계정(owner) ────────────────────────────────────────────────
  const ADMIN_COLUMNS = [
    { key: 'username', label: '계정명' }, { key: 'role', label: '역할' },
    { key: 'is_active', label: '활성', format: (value) => (value ? '활성' : '비활성') }, { key: 'password_changed_at', label: '비밀번호 변경일', format: moment },
    { key: 'created_at', label: '생성일', format: moment },
  ];

  function openAdmin(row) {
    openDialog(`관리자 ${row.username}`, row, ADMIN_COLUMNS);
    const edit = editForm({
      title: '역할·활성', fields: [{ key: 'role', label: '역할', kind: 'role' }, { key: 'is_active', label: '활성', kind: 'checkbox' }], row,
      path: `/admin/api/admins/${encodeURIComponent(row.id)}`,
      after: () => ' 역할 변경·비활성화는 대상의 모든 세션을 종료합니다.',
    });
    const reset = buildForm('비밀번호 재설정', [{ key: 'new_password', label: '새 비밀번호', kind: 'password' }], {}, '재설정');
    reset.onSubmit(async (values) => {
      const result = await api(`/admin/api/admins/${encodeURIComponent(row.id)}/password`, jsonBody('POST', values));
      reset.fill({});
      return `재설정했습니다. 종료된 세션 ${result.revoked_sessions ?? 0}개.`;
    });
    actions().append(edit.form, reset.form);
  }

  function loadAdmins() {
    $('admins').replaceChildren(el('div', 'placeholder', '관리자 목록을 불러오는 중…'));
    return api('/admin/api/admins').then(
      (data) => {
        $('admins-info').textContent = `${data.items.length}명`;
        $('admins').replaceChildren(data.items.length ? table(ADMIN_COLUMNS, data.items, openAdmin) : el('div', 'placeholder', '관리자 계정이 없습니다.'));
      },
      (error) => { if (error.name !== 'AbortError') $('admins').replaceChildren(el('p', 'error', error.message)); },
    );
  }

  /** 계정 생성 — 표 머리의 버튼이 옆 패널에 폼을 연다. 역할 기본값은 가장 낮은 권한. */
  $('admin-create').onclick = () => {
    openDialog('계정 생성');
    const fields = [{ key: 'username', label: '계정명', kind: 'text' }, { key: 'password', label: '초기 비밀번호', kind: 'password' }, { key: 'role', label: '역할', kind: 'role' }];
    const built = buildForm('새 관리자', fields, { role: state.roles[0] }, '계정 생성');
    built.nodes.username.required = built.nodes.password.required = true;
    built.nodes.username.autocomplete = 'off';
    built.onSubmit(async (values) => {
      const created = await api('/admin/api/admins', jsonBody('POST', { ...values, username: values.username.trim() }));
      built.fill({ role: state.roles[0] });
      loadAdmins();
      return `${created.username} (${created.role}) 계정을 만들었습니다.`;
    });
    actions().append(built.form);
  };

  return { userPanel, starterPanel, starterTools, openPasswordChange, loadAdmins };
}
