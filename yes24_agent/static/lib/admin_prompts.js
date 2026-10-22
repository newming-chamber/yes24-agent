// 프롬프트 버전(관리자 전용) — 왼쪽 버전 목록 · 오른쪽 미리보기(잠긴 영역 회색, 슬롯 textarea)·새 버전 저장·승격·롤백·폐기·이전 대비 diff
// (설계 docs/admin-lab-design-20261019.md §2.4). 판정·상태·해시는 서버 응답(admin_prompts.py)이 정본이고 이 화면은 그린다.
// 승격·롤백은 기존 모델 설정 PATCH(changes.prompt_versions) — 게이트(첫 승격 = owner 사유 메모 필수)·base_hash 판정은
// 서버(admin_prompts.check_prompt_bindings)다. 역할 판정은 서버(403). DB 값은 textContent·value로만(마크업 삽입 금지), 인라인 style 금지(CSP).
const jsonBody = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
/** 프롬프트 버전 상태(서버 파생 값) 표시. */
const PROMPT_STATUS = { active: '활성', draft: '초안', retired: '폐기' };
/** 모델별 바인딩의 '모든 모델' 키(서버 model_settings.prompt_versions 규칙). */
const ANY_MODEL = '*';

export function initPrompts({ $, el, api, table, analysisCard, openDialog, confirmDialog, showToast, errorText, placeholder }) {
  const state = { prompt: null, seq: 0 };
  const button = (text, onclick, cls = 'crema-button') => { const node = el('button', cls, text); node.type = 'button'; node.onclick = onclick; return node; };
  const head = (title, ...tools) => { const bar = el('div', 'section-head'); const span = el('span', 'table-tools'); span.append(...tools); bar.append(el('h3', null, title), span); return bar; };
  const keyLabel = (key) => (key === ANY_MODEL ? '모든 모델' : key);

  /** 한 화면 그리기 — 읽기·그리기 순서가 뒤집히지 않게 마지막 요청만 그린다. */
  async function draw(load, render) {
    const mine = ++state.seq;
    $('prompts').replaceChildren(placeholder('불러오는 중…'));
    try {
      const data = await load();
      if (mine === state.seq) render(data);
    } catch (error) {
      if (mine !== state.seq || error.name === 'AbortError') return;
      $('prompts').replaceChildren(placeholder(errorText(error), button('다시 시도', () => draw(load, render), 'link-button')));
    }
  }

  // ── 폼 도우미 — 패널 이탈 확인(admin.js panelDirty)은 form.dirty()를 본다.
  const dirtyOf = (form) => () => [...form.elements].some((node) => 'defaultValue' in node && node.type !== 'button' && node.type !== 'submit' && node.value !== node.defaultValue);
  function input(name, { type = 'text', value, required = false, attrs = {} } = {}) {
    const node = el(type === 'textarea' ? 'textarea' : 'input');
    if (type !== 'textarea') node.type = type;
    node.name = name;
    node.required = required;
    if (value != null) node.defaultValue = String(value);
    for (const [key, v] of Object.entries(attrs)) if (v != null) node.setAttribute(key, String(v));
    return node;
  }
  function field(label, node, help) {
    const wrap = el('label', null, label);
    wrap.append(node);
    if (help) wrap.append(el('small', 'field-help', help));
    return wrap;
  }
  const selectOf = (options, value) => { const node = el('select'); node.append(...options.map(([v, text]) => new Option(text, v))); if (value != null) node.value = value; return node; };
  /** 폼 제출 — 오류는 폼 끝 경고 줄에(서버 detail 그대로), 성공하면 dirty를 끈다. */
  function bindSubmit(form, run) {
    const warn = el('p', 'field-warn'), save = form.querySelector('button[type=submit]');
    form.append(warn);
    form.dirty = dirtyOf(form);
    form.onsubmit = async (event) => {
      event.preventDefault();
      warn.textContent = '';
      save.disabled = true;
      try { await run(); form.dirty = () => false; } catch (error) { if (error.name !== 'AbortError') warn.textContent = errorText(error, '저장하지 못했습니다.'); } finally { save.disabled = false; }
    };
  }
  const submitButton = (text) => { const node = el('button', 'primary', text); node.type = 'submit'; return node; };

  function load() {
    const read = () => Promise.all([api('/admin/api/prompts'), api('/admin/api/models')]);
    return draw(read, ([data, models]) => {
      const ctx = { data, models, defaults: Object.fromEntries(data.slots.map((slot) => [slot.name, slot.default])) };
      const columns = [{ key: 'name', label: '이름' }, { key: 'model', label: '모델', format: (v) => v ?? '모든 모델' },
        { key: 'status', label: '상태', format: (v) => PROMPT_STATUS[v] ?? v },
        { key: 'bound_to', label: '활성', format: (keys) => keys.map(keyLabel).join(', ') || '-' },
        { key: 'base_current', label: '코드 기본', format: (v) => (v ? '일치' : '바뀜') }];
      const editor = el('div', 'prompt-editor'), list = el('div', 'prompt-list');
      const pick = (version) => { state.prompt = version?.id ?? null; promptEditor(editor, ctx, version); };
      list.append(head('버전', button('+ 코드 기본에서 새 버전', () => pick(null))),
        data.versions.length ? table(columns, data.versions, pick, {}) : placeholder('저장된 버전이 없습니다.'));
      const bound = Object.entries(data.bindings).map(([key, id]) => `${keyLabel(key)} → #${id}`).join(' · ') || '없음(전 모델 코드 기본)';
      const grid = el('div', 'prompt-grid');
      grid.append(list, editor);
      $('prompts').replaceChildren(el('p', 'crema-note', `현재 바인딩: ${bound} · 코드 기본 해시 ${data.base_hash}`), grid);
      pick(data.versions.find((v) => v.id === state.prompt) ?? null);
    });
  }

  /** 편집기 — 버전을 고르면 서버 조립 미리보기(조각: 잠긴 영역 slot=null · 슬롯 영역)의 슬롯 자리에 textarea, 아니면 코드 기본 슬롯만. */
  async function promptEditor(box, { data, models, defaults }, version) {
    const token = box.token = {};
    const form = el('form', 'manage-form prompt-form');
    const model = selectOf([['', '모든 모델'], ...Object.entries(models.selectable).map(([label, id]) => [id, `${label} (${id})`])], version?.model ?? '');
    const areas = new Map(data.slots.map((slot) => [slot.name, input(`slot_${slot.name}`, { type: 'textarea', value: version?.slots?.[slot.name] ?? slot.default, attrs: { maxlength: data.slot_max_chars } })]));
    const preview = el('div', 'prompt-preview');
    form.append(el('h3', null, version ? `${version.name} (#${version.id}) 기반 새 버전` : '코드 기본에서 새 버전'), field('새 이름', input('name', { required: true })), field('대상 모델', model), field('메모', input('note', { value: version?.note })), preview);
    if (version) {
      preview.append(placeholder('미리보기를 불러오는 중…'));
      try {
        const p = await api(`/admin/api/prompts/${version.id}/preview`);
        if (box.token !== token) return;  // 미리보기를 기다리는 사이 다른 버전을 골랐다
        preview.replaceChildren(...p.segments.map((segment) => (segment.slot == null ? el('pre', 'prompt-locked', segment.text) : field(`슬롯 ${segment.slot}${segment.overridden ? ' · 이 버전 값' : ' · 코드 기본'}`, areas.get(segment.slot)))));
      } catch (error) { if (error.name === 'AbortError' || box.token !== token) return; preview.replaceChildren(el('p', 'field-warn', errorText(error, '미리보기를 불러오지 못했습니다.'))); }
    }
    for (const [name, area] of areas) if (!area.parentNode) preview.append(field(`슬롯 ${name}`, area));
    form.append(submitButton('새 버전으로 저장'));
    bindSubmit(form, async () => {
      // 코드 기본과 같은 슬롯은 보내지 않는다 — 오버레이 없음 = 코드 그대로.
      const slots = Object.fromEntries([...areas].map(([name, area]) => [name, area.value]).filter(([name, value]) => value !== defaults[name]));
      const res = await api('/admin/prompts', jsonBody('POST', { name: form.elements.name.value.trim(), model: model.value || null, slots, parent_id: version?.id ?? null, note: form.elements.note.value.trim() || null }));
      state.prompt = res.id;
      showToast(`프롬프트 버전 #${res.id}을 저장했습니다.`);
      load();
    });
    const parent = version?.parent_id != null && data.versions.find((v) => v.id === version.parent_id);
    box.replaceChildren(...(version ? [promptActions(data, models, version)] : []), form, ...(parent ? [slotDiff(parent, version, defaults)] : []));
  }

  /** 버전 동작 — 승격/롤백(모델 키별) · 바인딩 해제 · 폐기. */
  function promptActions(data, models, version) {
    const box = el('div', 'prompt-actions');
    box.append(el('p', 'crema-note', [PROMPT_STATUS[version.status] ?? version.status, version.model ? `모델 ${version.model}` : '모든 모델',
      version.activated_at ? '이전에 활성화됨(롤백 대상)' : '', version.base_current ? '' : '코드 기본이 바뀐 뒤 만든 버전이 아님'].filter(Boolean).join(' · ')));
    // 모델 전용 버전은 그 모델 키에만, 모든 모델 버전은 '*' 키에만 묶인다(서버 check_prompt_bindings 규칙).
    const key = version.model ?? ANY_MODEL;
    const tools = el('span', 'table-tools');
    if (!version.retired_at && data.bindings[key] !== version.id) tools.append(button(`${version.activated_at ? '롤백(다시 활성)' : '승격'} · ${keyLabel(key)}`, () => promote(data, models, key, version)));
    if (data.bindings[key] === version.id) tools.append(button(`바인딩 해제 · ${keyLabel(key)}(코드 기본)`, () => promote(data, models, key, null)));
    if (!version.retired_at && !version.bound_to.length) tools.append(button('폐기', () => retire(version)));
    box.append(tools);
    return box;
  }
  /** 바인딩 저장 — expected = 모델 탭이 본 저장 행 값(정본 직렬화 문자열), 다른 관리자가 먼저 바꿨으면 409. reason은 첫 승격 사유(서버 게이트). */
  async function bind(models, key, version, next, reason) {
    const res = await api('/admin/models', jsonBody('PATCH', { changes: { prompt_versions: Object.keys(next).length ? next : null }, expected: { prompt_versions: models.stored?.prompt_versions ?? null }, ...(reason ? { reason } : {}) }));
    const stale = (res.warnings ?? []).filter((w) => w.kind === 'base_hash_mismatch').map((w) => `#${w.version_id}`);
    showToast([version ? `${keyLabel(key)} → #${version.id}` : `${keyLabel(key)} 코드 기본`, stale.length ? `코드 기본이 바뀐 뒤 만든 버전이 아님 ${stale.join(', ')}` : ''].filter(Boolean).join(' · '));
    load();
  }
  async function promote(data, models, key, version) {
    const next = { ...data.bindings };
    if (version) next[key] = version.id; else delete next[key];
    const warn = version && !version.base_current;
    const change = version ? `${keyLabel(key)}의 답변이 프롬프트 버전 #${version.id}(${version.name})로 바뀝니다. 서버들은 설정 캐시 주기 안에 따라옵니다.` : `${keyLabel(key)}의 프롬프트 바인딩을 풀어 코드 기본으로 답합니다.`;
    const caution = warn ? '경고: 코드 기본이 바뀐 뒤 만든 버전이 아닙니다.' : '';
    if (version && !version.activated_at) {
      // 첫 승격 — 승격 사유 메모가 게이트다(감사 로그 after.promotion_reason에 남는다). 옆 패널 폼으로 받는다.
      if (!(await openDialog(`승격 · ${version.name}`, null))) return;
      const form = el('form', 'manage-form');
      form.append(el('h3', null, '첫 승격'), el('p', 'crema-note', [change, caution].filter(Boolean).join(' ')),
        field('승격 사유', input('reason', { type: 'textarea', required: true, attrs: { maxlength: 500 } }), '필수 — 어떤 검증(로컬 배터리·도그푸딩)으로 이 버전을 골랐는지. 감사 로그에 남습니다.'),
        submitButton(warn ? '경고를 확인했고 승격' : '승격'));
      bindSubmit(form, async () => { await bind(models, key, version, next, form.elements.reason.value.trim()); $('record-dialog').close(); });
      $('record-actions').replaceChildren(form);
      return;
    }
    const ok = await confirmDialog({ title: version ? '롤백' : '바인딩 해제', message: [change, version ? '이전에 활성화된 버전이라 게이트 없이 되돌립니다.' : '', caution].filter(Boolean).join(' '), danger: warn, confirmLabel: warn ? '경고를 확인했고 진행' : '확인' });
    if (!ok) return;
    try { await bind(models, key, version, next); } catch (error) { if (error.name !== 'AbortError') showToast(errorText(error, '바꾸지 못했습니다.')); }
  }
  async function retire(version) {
    const ok = await confirmDialog({ title: '버전 폐기', danger: true, confirmLabel: '폐기', message: `#${version.id}(${version.name})을 폐기합니다. 폐기된 버전은 승격할 수 없습니다.` });
    if (!ok) return;
    try { await api(`/admin/prompts/${version.id}`, jsonBody('PATCH', { changes: { retired_at: true }, expected: { retired_at: false } })); showToast('폐기했습니다.'); load(); } catch (error) { if (error.name !== 'AbortError') showToast(errorText(error, '폐기하지 못했습니다.')); }
  }
  /** 이전(부모) 버전 대비 바뀐 슬롯 — 이전 · 이번 두 칸(빈 슬롯은 코드 기본). */
  function slotDiff(parent, version, defaults) {
    const textOf = (v, name) => v.slots?.[name] ?? defaults[name] ?? '';
    const changed = Object.keys(defaults).filter((name) => textOf(parent, name) !== textOf(version, name));
    const box = el('div');
    if (!changed.length) box.append(el('p', 'crema-note', '슬롯 변경 없음'));
    for (const name of changed) {
      const pair = el('div', 'prompt-diff');
      pair.append(field('이전', el('pre', 'prompt-locked', textOf(parent, name))), field('이번', el('pre', 'prompt-slot', textOf(version, name))));
      box.append(el('h4', null, `슬롯 ${name}`), pair);
    }
    return analysisCard(`이전 버전 #${parent.id} 대비`, box);
  }

  return { load };
}
