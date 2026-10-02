import { initManage, roleLabel } from "/static/lib/admin_manage.js?v=33";
import { initCharts } from "/static/lib/admin_charts.js?v=9";
import { renderBody } from "/static/lib/md.js";
import { coverUrl, formatPrice, isSafeUrl, makeCoverImg, sourceCardType, sourceDomain, sourceTitle, CARD_LABELS } from "/static/lib/sources.js";

  const $ = (id) => document.getElementById(id);
  const state = { page: 0, tz: null, tab: 'dashboard', statistics: null, statisticsView: 'all', startersView: 'calendar', usersPage: 0, startersPage: 0, auditPage: 0, sort: {}, applied: {}, currentSession: null, listScroll: 0, username: null, role: null, roles: [], passwordMin: 0, changeMinBase: 0, excludedAccounts: 0, rbtiNames: {}, includeInternal: false, starterSlots: [], starterSummary: null, preview: null, openSlots: new Set(), slotQuery: '', pinnedSlotsOnly: false, popularShown: 20, calendar: null, calMonth: null, range: null, pendingMember: null, pendingStarter: null, dayRestore: null, pendingPin: null, periodFrom: null };
  const queryForms = {
    sessions: {form: 'filters', page: 'page', fields: {q: 'q', since: 'since', until: 'until', rating: 'rating', status: 'status', rbti: 'sessions-rbti'}},
    users: {form: 'users-filters', page: 'usersPage', fields: {q: 'users-q', rbti: 'users-rbti', nickname: 'users-nick'}},
    starters: {form: 'starters-filters', page: 'startersPage', fields: {q: 'starters-q', status: 'starters-status', source: 'starters-source', term: 'starters-term', slot: 'starters-slot', id: 'starters-id'}},
    audit: {form: 'audit-filters', page: 'auditPage', fields: {since: 'audit-since', until: 'audit-until', target: 'audit-kind', actor: 'audit-actor'}},
  };
  const pending = new Map();

  /** 텍스트를 노드로 넣어 항상 이스케이프한다(대화 본문에 마크업이 섞여도 안전). */
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  /** 턴 종료 상태 라벨(완료가 아닌 턴의 배지·답변거절 세부). */
  const statusLabel = (status) => ({ failed: '실패', interrupted: '중단', unknown: '종료 상태 미확인' })[status] || status;
  /** 피드백 평가 라벨(DDL ck_turn_feedback_rating: up/down) — 목록 배지·필터·상세·통계 공용. */
  const FEEDBACK_LABELS = { up: '좋아요', down: '싫어요' };
  /** 용어 통일표(한 곳) — 화면 라벨·CSV 머리가 이 말을 쓴다. 통계 일별 11열 엑셀 머리만 고객 양식이라 STAT_COLUMNS.excel. */
  const TERMS = { turns: '질의 수', users: '활성 사용자', clicks: '링크 클릭 수', sessions: '세션' };
  /** 답변거절 세부(서버 refused = {상태: 수}) — '실패 n · 중단 n'. 상태 목록은 서버 REFUSED가 정본이다. */
  const refusedText = (refused) => Object.entries(refused ?? {}).map(([status, n]) => `${statusLabel(status)} ${num(n)}`).join(' · ');
  const num = (n) => (n ?? 0).toLocaleString('ko-KR');
  // ── 어드민 표시 시간대 — /admin/api/me의 timezone(설정 admin_utc_offset_hours·라벨)이 정본이다.
  // 고정 오프셋이라 epoch를 옮긴 뒤 UTC 달력으로 읽는다(브라우저 시간대·tz 데이터베이스와 무관).
  const shifted = (ms) => new Date(ms + state.tz.offset_hours * 3600e3);
  /** 어드민 시간대 날짜 'YYYY-MM-DD' — 기본은 오늘, daysAgo일 전. */
  const localDay = (daysAgo = 0) => shifted(Date.now() - daysAgo * 864e5).toISOString().slice(0, 10);
  /** 기간 프리셋 하나의 정의 — 숫자 N은 '오늘 포함 최근 N일'(1 = 오늘, 운영자는 오늘을 본다), month는 이번 달 1일~오늘,
   *  last-month는 지난달 전체. 대시보드·통계·대화가 같은 계산을 쓴다. [since, until] 'YYYY-MM-DD'. */
  function presetRange(key) {
    if (/^\d+$/.test(key)) return [localDay(Number(key) - 1), localDay()];
    if (key === 'yesterday') return [localDay(1), localDay(1)];
    const [y, m] = localDay().split('-').map(Number), day = (date) => date.toISOString().slice(0, 10);
    return key === 'month' ? [day(new Date(Date.UTC(y, m - 1, 1))), localDay()] : [day(new Date(Date.UTC(y, m - 2, 1))), day(new Date(Date.UTC(y, m - 1, 0)))];
  }
  // 기간 프리셋 — 대시보드·통계·대화·감사가 이 목록 하나로 버튼을 만든다(값 = presetRange 키). 대화·감사는
  // 맨 앞에 '전체 기간'(data-all 글자, 값 '')이 붙고, 모두 끝에 '직접'(custom)이 붙는다.
  const PERIOD_PRESETS = [['1', '오늘'], ['yesterday', '어제'], ['7', '최근 7일'], ['30', '최근 30일'], ['month', '이번 달'], ['last-month', '지난 달']];
  for (const group of document.querySelectorAll('.period-presets')) {
    const all = group.dataset.all ? [['', group.dataset.all]] : [];
    group.append(...[...all, ...PERIOD_PRESETS, ['custom', '직접']].map(([range, label], i) => {
      const button = el('button', null, label);
      button.type = 'button';
      button.dataset.range = range;
      button.setAttribute('aria-pressed', String(i === 0 && all.length > 0));
      return button;
    }));
  }
  /** 세그먼트 버튼 묶음에서 하나만 눌림 표시. */
  const press = (group, pressed) => { for (const button of group.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button === pressed)); syncSegmentSelect(group); };
  /** 아주 좁은 화면(≤480) — 기간 세그먼트 옆에 같은 선택지의 선택 상자를 두고 CSS로 바꿔 보인다.
   *  세그먼트가 정본이다: 상자를 바꾸면 그 버튼을 누르고, 버튼이 눌리면(press·setPeriod) 상자 값을 맞춘다. */
  function segmentSelect(group) {
    const select = el('select', 'segment-select');
    select.setAttribute('aria-label', group.getAttribute('aria-label') ?? '기간 선택');
    select.append(...[...group.children].map((button, i) => new Option(button.textContent, String(i))));
    select.onchange = () => group.children[Number(select.value)].click();
    group.classList.add('has-select');
    group.after(select);
    group.segmentSelect = select;
    syncSegmentSelect(group);
  }
  function syncSegmentSelect(group) {
    const select = group.segmentSelect;
    if (select) select.value = String([...group.children].findIndex((button) => button.getAttribute('aria-pressed') === 'true'));
  }
  /** epoch 초 → 어드민 시간대 'YYYY-MM-DD HH:mm'(seconds면 :ss까지 — 대화 상세 기본정보만).
   *  시간대 라벨은 칸마다 붙이지 않는다 — 화면 머리 안내 문구(.tz)가 한 번 말한다. */
  const stamp = (ts, seconds = false) => (ts ? shifted(ts * 1000).toISOString().slice(0, seconds ? 19 : 16).replace('T', ' ') : '');

  /** 오류 문구는 서버 `detail`이 정본이다 — 상태코드별 문구를 여기서 다시 만들지 않는다. */
  async function api(path, {responseType, ...options} = {}) {
    const res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options });
    // 로그인한 화면에서 세션이 끊기면 페이지를 통째로 새로 연다 — 화면 상태를 손으로 비우지 않는다.
    if (res.status === 401 && state.role) { location.replace('/admin'); throw new DOMException('', 'AbortError'); }
    // 403은 이 탭의 역할이 낡았을 수 있다(다른 탭에서 계정 전환) — 계정이 바뀌었으면 새로 그리고, 같으면 오류 그대로.
    if (res.status === 403 && state.role && await accountChanged()) throw new DOMException('', 'AbortError');
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join(' · ') : body.detail;
      throw Object.assign(new Error(detail || `요청 실패 (${res.status})`), { status: res.status, body, response: res });
    }
    return responseType === 'response' ? res : res.status === 204 ? null : res.json();
  }

  /** 세션 쿠키는 브라우저 전체가 공유한다 — 다른 탭에서 다른 계정으로 로그인하면 이 탭의 state.role은 낡는다.
   *  /me를 다시 읽어 계정·역할이 다르면(로그아웃 포함) 현재 계정 기준으로 다시 그린다. 확인 실패는 판정 보류. */
  async function accountChanged() {
    const me = await fetch('/admin/api/me', { credentials: 'same-origin', cache: 'no-store' })
      .then((res) => (res.status === 401 ? null : res.ok ? res.json() : undefined), () => undefined);
    const changed = me !== undefined && (me?.username !== state.username || me?.role !== state.role);
    if (changed) location.reload();
    return changed;
  }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && state.role) accountChanged(); });

  /** 자리표시 문구 + (있으면) 동작 버튼 하나 — 오류의 '다시 시도', 빈 목록의 '조건 초기화'. */
  const placeholder = (text, action) => { const box = el('div', 'placeholder', text); if (action) box.append(' ', action); return box; };
  /** 빈 목록 — 걸린 조건이 있으면 '조건 초기화' 버튼을 붙인다(조건 폼이 있는 목록만). */
  function emptyList(name, text) {
    if (!queryForms[name] || !Object.keys(state.applied[name] ?? {}).length) return placeholder(text);
    const reset = el('button', 'link-button', '조건 초기화');
    reset.type = 'button';
    reset.onclick = () => resetQuery(name);
    return placeholder(text, reset);
  }
  // ── 내부·테스트 계정 제외(서버 설정 admin_excluded_user_ids) — 지표·목록 4화면 공통, 기본은 제외.
  // '포함해서 보기'는 한 곳의 상태(state.includeInternal)이고 주소에 internal=1로 남는다. 설정이 비면 토글이 없다.
  const SCOPED_TABS = ['dashboard', 'statistics', 'sessions', 'users', 'starters'];
  const scope = () => (state.includeInternal ? { internal: 1 } : {});
  function renderInternalToggles() {
    for (const node of document.querySelectorAll('.internal-toggle')) {
      node.hidden = !state.excludedAccounts;
      const button = el('button', 'link-button', state.includeInternal ? '제외하고 보기' : '포함해서 보기');
      button.type = 'button';
      button.onclick = () => {
        state.includeInternal = !state.includeInternal;
        if (PAGE_KEYS[state.tab]) state[PAGE_KEYS[state.tab]] = 0;  // 범위가 바뀌면 첫 페이지부터
        renderInternalToggles();
        writeHash();
        loadOverview();
        loaders[state.tab]();
      };
      node.replaceChildren(state.includeInternal ? `내부 계정 ${num(state.excludedAccounts)}개 포함 중 · ` : `내부 계정 ${num(state.excludedAccounts)}개 제외 · `, button);
    }
  }
  /** 정의 표시 (i) — 지표 이름은 고객 보고 양식 그대로 두고 뜻은 여기서 말한다(title + 스크린리더 글자). */
  const REFUSAL_HINT = '답변거절 = 실패·중단(응답이 끝나지 않은 질문)';
  /** 숫자마다의 정의 한 줄(정의 표 한 곳) — 카드·KPI는 (i), 표는 머리 툴팁으로 보인다. 지표 이름은 고객 양식 그대로. */
  const METRIC_HINTS = {
    sessions: '세션 = 대화방 하나(질문 여러 개가 한 세션). 그 날짜에 질문이 있었던 대화방 수예요.',
    users: '활성 사용자 = 이 기간에 실제로 질문한 사람 수(같은 사람은 한 번). 기간 값은 기간 안의 고유 수라, 날마다 센 값을 더한 것보다 작을 수 있어요.',
    queries: '질의 = 사용자가 보낸 질문 수.',
    queries_per_session: '세션당 평균 질의 수 = 질의 수 ÷ 세션 수.',
    avg_session_seconds: '평균 세션 시간 = 대화방 하나에서 첫 질문부터 마지막 답변까지 걸린 시간의 평균(날짜별).',
    refusals: REFUSAL_HINT,
    links: '링크 제공 = 답변에 출처로 보인 링크 수.',
    clicks: '링크 클릭 = 그 링크를 누른 수(같은 링크를 다시 눌러도 셉니다).',
    click_rate: '클릭률 = 링크 클릭 수 ÷ 링크 제공 수.',
    feedback: '피드백 = 답변에 남긴 좋아요·싫어요 수(평가한 시각 기준).',
    cost: '비용 = 모델 사용량에 단가를 곱한 예상치(USD). 실제 청구액과 다를 수 있어요.',
    cost_per_turn: '대화 질의당 비용 = 대화 비용 ÷ 질의 수(대화 외 작업 비용은 빼고, 비용이 계산된 질의만 셉니다).',
    rbti_rate: 'RBTI 적용률 = RBTI 유형이 적용된 질의 ÷ 기간 전체 질의.',
    rbti_users: '회원 수 = 이 기간에 그 유형으로 대화한 사람 수(기간 중 유형이 바뀐 사람은 두 유형에 모두 셉니다).',
    uses: '사용 수 = 최근 새 대화 중 첫 질문이 이 문장과 똑같았던 대화 수(첫 화면 질문을 눌러 시작한 횟수).',
    member_turns: '질의 수(누적) = 이 회원이 지금까지 보낸 질문 수.',
  };
  // (i) 정의 — 누르거나(모바일 탭) Enter로 여는 작은 팝오버 하나를 화면 전체가 같이 쓴다. title 툴팁은
  // 터치 기기에서 볼 수 없어서다. 카드(버튼) 안에도 놓이므로 버튼 대신 role=button 글자로 두고, 누름이 카드로
  // 번지지 않게 막는다. 바깥을 누르거나 Esc·스크롤이면 닫힌다.
  const infoPop = Object.assign(el('div', 'info-pop'), { hidden: true });
  infoPop.setAttribute('role', 'tooltip');
  infoPop.id = 'info-pop';
  document.body.append(infoPop);
  function infoMark(text) {
    const mark = el('span', 'info-mark', 'i');
    mark.dataset.hint = text;
    mark.tabIndex = 0;
    mark.setAttribute('role', 'button');
    mark.setAttribute('aria-label', `설명 보기: ${text}`);
    const toggle = (event) => { event.preventDefault(); event.stopPropagation(); infoPop.owner === mark && !infoPop.hidden ? closeInfo() : openInfo(mark); };
    mark.onclick = toggle;
    mark.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') toggle(event); };
    return mark;
  }
  function openInfo(mark) {
    infoPop.textContent = mark.dataset.hint;
    infoPop.owner = mark;
    infoPop.hidden = false;
    mark.setAttribute('aria-describedby', 'info-pop');
    const at = mark.getBoundingClientRect(), width = infoPop.offsetWidth;
    infoPop.style.setProperty('--x', `${Math.max(8, Math.min(at.left - 8, innerWidth - width - 8))}px`);
    infoPop.style.setProperty('--y', `${at.bottom + 6}px`);
  }
  function closeInfo() {
    infoPop.owner?.removeAttribute('aria-describedby');
    infoPop.hidden = true;
    infoPop.owner = null;
  }
  document.addEventListener('click', (event) => { if (!infoPop.hidden && !infoPop.contains(event.target)) closeInfo(); });
  document.addEventListener('scroll', () => { if (!infoPop.hidden) closeInfo(); }, true);
  /** 상단 오류 띠 — retry가 있으면 끝에 '다시 시도'(누르면 띠를 닫고 다시 읽는다). */
  function showError(message, retry) {
    const banner = $('app-error'), again = el('button', 'link-button', '다시 시도');
    again.type = 'button';
    again.onclick = () => { banner.hidden = true; retry(); };
    banner.replaceChildren(message, ...(retry ? [' · ', again] : []));
    banner.hidden = false;
  }
  /** 사용자에게 보일 오류 문구 — 우리가 만든 Error(서버 문구·폼 검사)만 그대로, 그 밖(TypeError 등 네트워크·
   *  스크립트 예외)은 console에만 남기고 fallback(원시 예외 문구를 화면에 내지 않는다). */
  const errorText = (error, fallback = '불러오지 못했습니다.') => { if (error.constructor === Error) return error.message; console.error(error); return fallback; };

  async function request(key, path, render) {
    pending.get(key)?.abort();
    const controller = new AbortController();
    pending.set(key, controller);
    try {
      const data = await api(path, { signal: controller.signal });
      if (!controller.signal.aborted) render(data);
    } catch (error) {
      if (error.name !== 'AbortError' && !controller.signal.aborted) {
        // 그릴 자리가 있으면 그 자리에(문구 + 다시 시도), 없으면 상단 띠에 — 같은 말을 두 곳에 띄우지 않는다.
        const again = () => request(key, path, render), target = $(key), message = errorText(error);
        if (!target) return showError(message, again);
        const bar = key === 'detail' ? target.querySelector('#detail-bar') : null;
        const retry = el('button', 'link-button', '다시 시도');
        retry.type = 'button';
        retry.onclick = again;
        target.replaceChildren(...(bar ? [bar] : []), placeholder(message, retry));
      }
    } finally {
      if (pending.get(key) === controller) pending.delete(key);
    }
  }

  function loadOverview() {
    return request('stats', `/admin/api/overview?${new URLSearchParams(scope())}`, (o) => {
      // 용어는 통계 탭과 같다(TERMS) — 활성 사용자 = 실제로 대화한 사용자, 세션 = 대화방(질문 여러 개가 한 세션).
      // 카드를 누르면 그 목록(회원 · 전체 대화)으로 간다.
      const stats = [['누적 이용자', num(o.chat_users), '실제로 대화한 사용자 수(전체 기간) · 누르면 회원 목록', () => switchTab('users')],
        [`누적 ${TERMS.sessions}`, num(o.sessions), '대화방 수(질문 여러 개가 한 세션) · 누르면 전체 대화', () => openSessions({})],
        [`누적 ${TERMS.turns}`, num(o.turns), '누르면 전체 대화', () => openSessions({})]];
      $('stats').replaceChildren(...stats.map(([label, value, hint, open]) => {
        const box = el('button', 'stat');
        box.type = 'button';
        box.append(el('b', null, value), el('span', null, label));
        box.title = hint;
        box.onclick = open;
        return box;
      }));
    });
  }

  /** 초 → "N분 N초"(통계 평균 세션시간·대화 지속시간 공용). */
  const minutes = (value) => { if (value == null) return '-'; const s = Math.round(value); return s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`; };

  /** 내려받기 한 곳 — 화면이 만든 CSV(대화 목록·통계)를 파일로 저장한다. */
  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = el('a');
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  /** 화면이 만드는 CSV의 셀 규칙 — 스프레드시트가 수식으로 읽는 선두 문자면 앞에 '를 붙인다.
   *  탭·CR은 원문 첫 글자로, 그 밖은 앞 공백을 벗긴 뒤 = + - @로 본다. */
  function csvCell(value) {
    let text = value == null ? '' : String(value);
    if (/^[\t\r]/.test(text) || /^[=+\-@]/.test(text.replace(/^\s+/, ''))) text = "'" + text;
    return `"${text.replaceAll('"', '""')}"`;
  }
  function saveCsv(filename, rows) {
    saveBlob(new Blob(['\ufeff' + rows.map((row) => row.map(csvCell).join(',')).join('\r\n')], { type: 'text/csv;charset=utf-8' }), filename);
  }

  /** 목록 건수 — '1–50 / 2,340'(이 페이지 범위 / 전체). 0건이면 '0건'. */
  const rangeText = (data) => (data.total ? `${num(data.page * data.page_size + 1)}–${num(data.page * data.page_size + data.items.length)} / ${num(data.total)}` : '0건');
  /** crema 페이지 버튼(QueryAnalysis.tsx): « 처음 · ‹ · 10개 묶음 · › · » 끝. page는 0부터. */
  function cremaPager(target, total, size, page, go) {
    // 0건이면 페이저를 숨기고 같은 목록의 CSV 다운로드도 끈다(받을 것이 없다). 버튼 id = 목록 키 + '-csv'.
    target.hidden = !total;
    const csv = $(target.id.replace(/-pager$/, '-csv'));
    if (csv) csv.disabled = !total;
    const pages = Math.max(1, Math.ceil(total / size)), first = Math.floor(page / 10) * 10, last = Math.min(pages, first + 10);
    const button = (label, to, aria) => {
      const node = el('button', null, label);
      node.type = 'button';
      if (aria) node.setAttribute('aria-label', aria);
      node.disabled = to < 0 || to >= pages;
      node.onclick = () => go(to);
      return node;
    };
    const numbers = [];
    for (let n = first; n < last; n++) {
      const node = button(String(n + 1), n);
      if (n === page) node.setAttribute('aria-current', 'page');
      numbers.push(node);
    }
    const edge = (label, to, aria) => { const node = button(label, to, aria); node.disabled = to === page; return node; };
    target.replaceChildren(edge('«', 0, '첫 페이지'), button('‹', page - 1, '이전 페이지'), ...numbers, button('›', page + 1, '다음 페이지'), edge('»', pages - 1, '마지막 페이지'));
  }

  /** 사용자 칸 — 닉네임(서버가 모르면 회원번호로 채워 준다) + 다르면 작게 회원번호. 둘 다 textContent. */
  function userCell(s) {
    const cell = el('span', 'q-user', s.nickname);
    if (s.nickname !== s.user_id) cell.append(el('small', null, s.user_id));
    return cell;
  }

  /** 같은 시각을 날짜·시각 두 줄로(목록 칸) — ['YYYY-MM-DD', 'HH:mm']. */
  const clockParts = (ts) => stamp(ts).split(' ');

  function loadSessions() {
    writeHash();
    const page = state.page;
    const sort = state.sort.sessions, params = new URLSearchParams({ ...state.applied.sessions, ...(sort ? { sort: sort.key, dir: sort.dir } : {}), ...scope(), page });
    $('sessions').replaceChildren(el('div', 'placeholder', '대화를 불러오는 중…'));
    return request('sessions', `/admin/api/sessions?${params}`, (data) => {
      $('page-info').textContent = rangeText(data);
      addMissingOptions($('sessions-rbti'), data.rbti_types, rbtiLabel);
      const rows = data.items.map((s) => {
        const row = el('button', 'query-row');
        row.type = 'button';
        row.dataset.id = s.id;
        row.setAttribute('aria-current', String(s.id === state.currentSession?.id));
        row.onclick = () => selectSession(s);
        // 두 줄 위계: ① 첫 질문 … 날짜 시각 ② 닉네임(회원번호) · 질의 n건 · 배지. 세션 id는 상세에만(툴팁에도).
        const top = el('span', 'q-top'), [day, time] = clockParts(s.update_time), when = el('span', 'q-time', day);
        when.append(el('small', null, time));
        top.append(el('b', s.preview ? 'q-title' : 'q-title empty', s.preview || '(사용자 발화 없음)'), when);
        const meta = el('span', 'q-meta', `${s.nickname === s.user_id ? s.user_id : `${s.nickname}(${s.user_id})`} · 질의 ${num(s.turn_count)}건`);
        // 그 세션에서 가장 최근에 적용된 RBTI — 누르면 그 유형으로 거른다(카드 전체가 버튼이라 배지는 em + 클릭; 키보드는 상세의 RBTI 버튼).
        if (s.rbti) { const badge = el('em', 'rbti', s.rbti); badge.title = `${rbtiLabel(s.rbti)}만 보기 · 다시 누르면 해제`; badge.onclick = (event) => { event.stopPropagation(); toggleRbti('sessions', s.rbti); }; meta.append(badge); }
        for (const [rating, key] of [['up', 'likes'], ['down', 'dislikes']]) if (s[key]) meta.append(el('em', `feedback-${rating}`, `${FEEDBACK_LABELS[rating]} ${num(s[key])}`));
        for (const [status, n] of Object.entries(s.refused ?? {})) if (n) meta.append(el('em', `status-${status}`, `${statusLabel(status)} ${num(n)}`));
        row.append(top, meta);
        row.title = `세션 ${s.id}`;
        return row;
      });
      $('sessions').replaceChildren(...(rows.length ? rows : [emptyList('sessions', '조건에 맞는 대화가 없습니다.')]));
      cremaPager($('sessions-pager'), data.total, data.page_size, page, (to) => { state.page = to; loadSessions(); $('sessions-list').scrollTop = $('sessions-pane').scrollTop = 0; });
      // 상세 칸을 비워 두지 않는다 — 아직 고른 대화가 없으면 첫 대화를 연다(좁은 화면은 목록에 머문다).
      if (!state.currentSession && data.items.length) selectSession(data.items[0], { reveal: false });
      else if (!state.currentSession) $('detail').replaceChildren(el('div', 'placeholder', '조건에 맞는 대화가 없습니다.'));
    });
  }

  /** CSV 다운로드 = 적용한 조건·정렬의 목록 전체(페이지를 이어 받아 UTF-8 BOM CSV로) — 대화·회원 공용.
   *  line(행, 순번, 전체 행)은 CSV 한 줄. 파일은 화면 밖으로 나가므로 시각 열 머리에 시간대를 적는다. */
  async function downloadList(button, name, path, head, line) {
    const rows = [], query = { ...state.applied[name], ...(state.sort[name] ? { sort: state.sort[name].key, dir: state.sort[name].dir } : {}), ...(SCOPED_TABS.includes(name) ? scope() : {}) };
    button.disabled = true;
    try {
      for (let page = 0; ; page++) {
        const data = await api(`${path}?${new URLSearchParams({ ...query, page })}`);
        rows.push(...data.items);
        if (rows.length >= data.total || !data.items.length) break;
      }
      saveCsv(`${name}_${localDay()}.csv`, [head, ...rows.map((row, i) => line(row, i, rows))]);
    } catch (error) { if (error.name !== 'AbortError') showError(`다운로드 실패: ${errorText(error, '다시 시도해 주세요.')}`); }
    finally { button.disabled = false; }
  }
  const downloadSessions = () => downloadList($('sessions-csv'), 'sessions', '/admin/api/sessions',
    ['No.', '세션 ID', '닉네임', '회원번호', '첫 질문', TERMS.turns, 'RBTI', FEEDBACK_LABELS.up, FEEDBACK_LABELS.down, statusLabel('failed'), statusLabel('interrupted'), `생성 (${state.tz.label})`, `최근 갱신 (${state.tz.label})`],
    (s, i, rows) => [rows.length - i, s.id, s.nickname, s.user_id, s.preview, s.turn_count, s.rbti, s.likes, s.dislikes, s.refused?.failed ?? 0, s.refused?.interrupted ?? 0, stamp(s.create_time), stamp(s.update_time)]);
  const downloadUsers = () => downloadList($('users-csv'), 'users', '/admin/api/users',
    ['닉네임', '회원번호', 'RBTI', `${TERMS.turns} (누적)`, `마지막 질의 (${state.tz.label})`, `첫 이용일 (${state.tz.label})`],
    (u) => [u.nickname, u.user_no, u.rbti, u.turns, stamp(u.last_chat_at), stamp(u.created_at)]);

  // ── 대화 상세(crema QueryDetail.tsx) ──────────────────────────────────────
  const copyButton = (text, label = '복사') => {
    const button = el('button', 'copy-btn', label);
    button.type = 'button';
    button.onclick = async () => {
      try { await navigator.clipboard.writeText(text); button.textContent = '복사됨'; }
      catch { button.textContent = '복사 실패'; }
    };
    return button;
  };
  const sectionBar = (title, note, tool) => {
    const bar = el('div', 'section-bar bleed'), label = el('span', null, `${title} `);
    if (note) label.append(el('small', null, note));
    bar.append(label);
    if (tool) bar.append(tool);
    return bar;
  };

  /** 인용 출처 카드 — 책·상품은 crema 추천 도서 카드 모양(왼쪽 표지 · 오른쪽 정보).
   *  출처 URL은 웹 검색에서 온 비신뢰 데이터라 http(s)만 링크로 연다(isSafeUrl). 표지는 coverUrl이
   *  http(s)만 통과시키고, 어드민 CSP img-src가 설정한 표지 출처(admin_image_origins) 밖은 막는다.
   *  표지를 못 불러오면 칸째 지우고 표지 칸 배치(has-cover)도 풀어 정보만 남는다. */
  function sourceCard(src) {
    const cover = coverUrl(src);
    const card = el('div', `source-card${cover ? ' has-cover' : ''}`);
    card.dataset.sourceId = String(src.id);
    if (cover) {
      const img = makeCoverImg(cover, 'source-cover', { onError: () => { img.remove(); card.classList.remove('has-cover'); } });
      card.append(img);
    }
    const tags = el('span');
    // 판형: 출처가 준 kind가 정본. 없을 때 is_ebook === true만 긍정 관측으로 쓴다(False·None은 판정 근거가 아니다).
    tags.append(el('em', null, String(src.id)), el('em', 'kind', src.kind || (src.is_ebook === true ? 'eBook' : CARD_LABELS[sourceCardType(src)])));
    card.append(tags, sourceTitleLink(src));
    const byline = [src.author, src.publisher].filter(Boolean).join(' | ');
    card.append(el('p', null, byline || sourceDomain(src) || src.url || ''));
    const facts = el('p');
    if (src.rating) { const rating = el('span', 'rating'); rating.append(el('b', null, '★'), ` ${src.rating}`); facts.append(rating, document.createTextNode(' ')); }
    const extra = [src.review_count ? `리뷰 ${num(src.review_count)}` : '', formatPrice(src) || ''].filter(Boolean).join(' · ');
    if (extra) facts.append(document.createTextNode(extra));
    if (facts.childNodes.length) card.append(facts);
    return card;
  }

  /** 출처 제목 — 안전한 url이면 새 창 링크, 아니면 글자만. */
  function sourceTitleLink(src) {
    const title = el(isSafeUrl(src.url) ? 'a' : 'span', null, sourceTitle(src));
    if (isSafeUrl(src.url)) { title.href = src.url; title.target = '_blank'; title.rel = 'noopener noreferrer'; }
    title.title = sourceTitle(src);
    return title;
  }

  /** 웹·안내 출처 한 줄 — 번호 · 제목 · 도메인(표지·서지 없음). */
  function sourceLine(src) {
    const row = el('div', 'source-line');
    row.dataset.sourceId = String(src.id);
    row.append(el('em', null, String(src.id)), sourceTitleLink(src), el('small', null, sourceDomain(src)));
    return row;
  }

  // 인용 출처 묶음 — 출처의 구조 분류(sources.js sourceCardType: card_type·type)로 나눈다. 도서는 표지 카드, 나머지는 한 줄.
  const SOURCE_GROUPS = [['book', '도서', sourceCard, 'source-cards'], ['link', '웹', sourceLine, 'source-lines'], ['document', 'Yes24 안내', sourceLine, 'source-lines']];

  /** 질의내역 한 칸 — 머리(질문·시각·지표)를 누르면 그 자리에 답변(md.js 렌더 + 인용 마커)과 출처 카드가 펼쳐진다. */
  function turnBlock(t, i) {
    const block = el('details', 'turn bleed');
    block.dataset.turnId = t.turn_id;
    const meta = el('small');
    if (t.status !== 'completed') meta.append(el('em', `status-${t.status}`, statusLabel(t.status)), ' ');
    // 배지(상태·평가·RBTI)는 목록 카드와 같은 클래스, 시각은 목록과 같은 형식(YYYY-MM-DD HH:mm).
    if (t.rating) meta.append(el('em', `feedback-${t.rating}`, FEEDBACK_LABELS[t.rating]), ' ');
    if (t.rbti_applied) meta.append(el('em', 'rbti', t.rbti_applied), ' ');
    meta.append([stamp(t.asked_at), t.elapsed_ms == null ? '' : `응답 ${seconds(t.elapsed_ms / 1000)}`,
      t.clicks ? `클릭 ${num(t.clicks)}` : ''].filter(Boolean).join(' · '));
    const head = el('summary', 'turn-head fold');
    head.append(el('em', null, `#${i + 1}`), el('b', null, t.user_message), meta);
    const tools = el('div', 'turn-tools');
    tools.append(copyButton(t.user_message, '질문 복사'), copyButton(t.assistant_message, '답변 복사'));
    const answer = el('div', 'answer');
    const body = el('div', 'turn-body');
    body.append(tools, answer);
    // 사용자가 평가와 함께 남긴 의견 — 답변 바로 아래 인용 블록(본문은 textContent).
    if (t.feedback_comment) {
      const quote = el('blockquote', `feedback-comment feedback-${t.rating}`);
      quote.append(el('b', null, `사용자 의견 · ${FEEDBACK_LABELS[t.rating] ?? ''}`), el('p', null, t.feedback_comment));
      body.append(quote);
    }
    const byId = new Map((t.sources || []).map((src) => [String(src.id), src]));
    const sources = el('details', 'sources');
    if (!t.assistant_message) answer.append(el('span', 'empty', '(응답 없음)'));
    // 마커는 이 턴의 공개(인용) 출처 id일 때만 배지로 승격되고, 안전한 url이면 링크다(md.js 계약).
    else renderBody(answer, t.assistant_message, {
      isCitation: (id) => byId.has(id),
      citationUrl: (id) => (isSafeUrl(byId.get(id)?.url) ? byId.get(id).url : ''),
      onMarker: (id) => {
        sources.open = true;
        for (const card of sources.querySelectorAll('[data-source-id]')) card.classList.toggle('is-target', card.dataset.sourceId === id);
        sources.querySelector(`[data-source-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: 'nearest' });
      },
    });
    // 중단된 턴은 본문이 중간에서 끝난다 — 끝에 그 사실을 적는다.
    if (t.status === 'interrupted') answer.append(el('p', 'interrupted-note', '— 여기서 응답이 중단됨'));
    if (t.sources?.length) {
      sources.open = true;
      const counts = [], groups = [];
      for (const [type, label, render, className] of SOURCE_GROUPS) {
        const items = t.sources.filter((src) => sourceCardType(src) === type);
        if (!items.length) continue;
        const list = el('div', className);
        list.append(...items.map(render));
        counts.push(`${label} ${num(items.length)}`);
        groups.push(el('h4', 'source-group', `${label} ${num(items.length)}`), list);
      }
      sources.append(el('summary', 'fold', ['인용 출처', ...counts].join(' · ')), ...groups);
      body.append(sources);
    }
    block.append(head, body);
    return block;
  }

  const syncFoldAll = () => {
    const all = $('fold-all');
    if (all) all.textContent = [...$('detail').querySelectorAll('details.turn')].every((b) => b.open) ? '모두 접기' : '모두 펼치기';
  };
  // 턴이 닫히면 머리(sticky)가 화면 위로 밀려 올라가 있을 수 있다 — 접힌 자리로 돌려 놓는다. toggle은 버블링하지 않아 캡처로 받는다.
  $('detail').addEventListener('toggle', (e) => {
    if (!e.target.classList.contains('turn')) return;
    if (!e.target.open) e.target.firstElementChild.scrollIntoView({ block: 'nearest' });
    syncFoldAll();
  }, true);

  /** 좁은 화면의 목록 ↔ 상세 한 칸 전환(넓은 화면은 늘 나란히라 영향이 없다). 한 칸일 땐 창이 스크롤하므로 목록 위치를 기억했다 돌려준다. */
  function showDetail(on) {
    const pane = $('sessions-pane');
    if (on === pane.classList.contains('has-selection')) return;
    if (on) state.listScroll = pane.scrollTop;
    pane.classList.toggle('has-selection', on);
    pane.scrollTop = on ? 0 : state.listScroll;
    // 목록이 가려지는 한 칸 화면에서만 기록을 쌓는다(넓은 화면은 나란히라 뒤로가기 대상이 아니다).
    if (on && !$('sessions-list').offsetParent && !history.state?.detail) history.pushState({ detail: true }, '', location.hash);
  }

  /** 상세 보기 — 첫 진입은 #1만 펼친다. preservePosition(새로고침)은 펼침·스크롤을 지킨다. */
  function selectSession(session, { preservePosition = false, reveal = !preservePosition } = {}) {
    const scrollTop = $('detail').scrollTop, infoOpen = !!$('detail').querySelector('details.info')?.open;
    const openTurns = new Set([...$('detail').querySelectorAll('details.turn[open]')].map((b) => b.dataset.turnId));
    state.currentSession = { ...session };
    writeHash();
    for (const row of $('sessions').querySelectorAll('.query-row')) row.setAttribute('aria-current', String(row.dataset.id === session.id));
    if (reveal) showDetail(true);  // 새로고침·자동 선택은 좁은 화면의 목록/상세 칸을 그대로 둔다
    const bar = el('div', 'detail-bar bleed'), back = el('button', 'detail-back', '← 목록');
    const meta = el('span', 'detail-meta', [session.id.slice(0, 8), session.nickname].filter(Boolean).join(' · '));
    bar.id = 'detail-bar';  // request()가 오류 화면에서도 이 막대는 남긴다
    back.type = 'button';
    back.onclick = () => (history.state?.detail ? history.back() : showDetail(false));
    bar.append(back, el('h2', null, '대화 상세'), meta);
    if (session.rbti) bar.append(rbtiButton('sessions', session.rbti));  // 목록 배지의 키보드 대체
    $('detail').replaceChildren(bar, el('div', 'placeholder', '대화를 불러오는 중…'));
    // 회원번호가 없으면(손으로 고친 주소) 세션 id만 — 서버가 찾고, 겹치면 409로 알린다.
    const params = new URLSearchParams(session.user_id ? { user_id: session.user_id } : {});
    return request('detail', `/admin/api/sessions/${encodeURIComponent(session.id)}?${params}`, (d) => {
      const m = d.metrics, turns = d.turns;
      const info = el('dl', 'info-grid bleed');
      // 사용자 → 회원 화면에서 그 회원 패널을 연다.
      const member = el('button', 'link-button', '회원 보기');
      member.type = 'button';
      member.onclick = () => openMember(d.session.user_id);
      const started = turns.length ? turns[0].asked_at : d.session.create_time;
      const rows = [
        ['세션 ID', d.session.id, copyButton(d.session.id)], ['사용자', userCell(d.session), copyButton(d.session.user_id), member],
        ['시작 일시', stamp(started, true)], [TERMS.turns, `${num(m.turns)}회`],
        ['평균 응답속도', seconds(m.avg_turn_seconds)], ['지속시간', minutes(m.duration_seconds)],
        // 지속시간과 같은 기준(chat_turn)의 마지막 응답 — sessions.update_time은 ADK 이벤트 기록 때 밀리는 값이라
        // 답변 완료(스트림 마감 뒤 기록)와 수 초 어긋난다(목록의 '최근 갱신'이 그 값이다).
        ['마지막 응답', turns.length ? stamp(Math.max(...turns.map((t) => t.completed_at)), true) : '-'],
      ];
      for (const [label, value, ...tools] of rows) { const dd = el('dd'); dd.append(value, ...tools); info.append(el('dt', null, label), dd); }
      const who = d.session.nickname === d.session.user_id ? d.session.user_id : `${d.session.nickname}/${d.session.user_id}`;
      meta.textContent = `${d.session.id.slice(0, 8)} · ${d.session.nickname}`;
      const summary = el('summary', 'section-bar bleed fold'), label = el('span', null, '기본정보 ');
      label.append(el('small', null, [who, `${stamp(started)} 시작`, `${num(m.turns)}회`, m.avg_turn_seconds == null ? '' : `평균 ${seconds(m.avg_turn_seconds)}`].filter(Boolean).join(' · ')));
      summary.append(label);
      const basics = el('details', 'info bleed');
      basics.open = infoOpen;
      basics.append(summary, info);
      if (!turns.length) { $('detail').replaceChildren(bar, basics, el('div', 'placeholder', '이 대화에는 기록된 턴이 없습니다.')); return; }
      const blocks = turns.map(turnBlock);
      // 피드백 필터(좋아요·싫어요)로 연 대화는 그 평가가 달린 첫 턴을 펼쳐 강조한다(대개 #1이 아니다).
      const rated = state.applied.sessions?.rating, target = rated && turns.findIndex((t) => t.rating === rated);
      if (preservePosition) for (const b of blocks) b.open = openTurns.has(b.dataset.turnId);
      else blocks[target > 0 ? target : 0].open = true;
      if (!preservePosition && target >= 0 && rated) blocks[target].classList.add('is-target');
      const turnsBar = sectionBar('질의내역', `${num(turns.length)}건`);
      if (turns.length > 1) {
        const all = el('button', 'fold-all');
        all.id = 'fold-all';
        all.type = 'button';
        all.onclick = () => { const open = all.textContent === '모두 펼치기'; for (const b of blocks) b.open = open; };
        turnsBar.append(all);
      }
      $('detail').replaceChildren(bar, basics, turnsBar, ...blocks);
      syncFoldAll();
      $('detail').scrollTop = preservePosition ? scrollTop : 0;
      if (!preservePosition && target > 0) blocks[target].scrollIntoView({ block: 'start' });
    });
  }

  const valueText = (value) => value == null ? 'NULL · 값 없음' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  /** 초 — 어디서나 소수 1자리(응답 시간·평균 응답속도·차트 눈금). */
  const seconds = (value) => value == null ? '미측정' : `${Number(value).toFixed(1)}초`;
  const metric = (value) => value == null ? '미측정' : num(value);

  /** 정렬 비교 — 원값 기준(숫자·불리언은 크기, 날짜 문자열·글자는 한국어 자연 순서). 빈 값은 방향과 무관하게 뒤로. */
  const compare = (a, b) => (typeof a === 'number' || typeof a === 'boolean' ? a - b : String(a).localeCompare(String(b), 'ko', { numeric: true }));
  const sortRows = (rows, key, dir) => [...rows].sort((x, y) => (x[key] == null) - (y[key] == null) || (x[key] == null ? 0 : compare(x[key], y[key]) * (dir === 'asc' ? 1 : -1)));

  /** 표. sorting이 있으면 열 머리를 눌러 정렬한다(오름 ↔ 내림, aria-sort 표시). lead는 머리 아래 고정 요약 행(정렬 밖).
   *  - sorting.onSort가 있으면 서버 정렬(페이지 목록) — 머리는 column.sort가 참인 열만, 누르면 onSort(key, dir).
   *  - 없으면 이 표 안에서 원값으로 정렬(한 번에 다 받은 표) — 모든 열. */
  /** 표 칸 하나 — 서식 결과나 원값이 노드면(시각 칸 등 — 기록 표는 서식된 노드를 값으로 받는다) 그대로 붙인다. */
  function cellOf(column, raw) {
    const value = column.format ? column.format(raw) : raw instanceof Node ? raw : valueText(raw);
    const td = el('td', column.numeric ? 'num' : null);
    td.dataset.label = column.label;  // 좁은 화면 카드형 표(회원)의 칸 이름
    td.dataset.key = column.key;  // 좁은 화면 두 줄 행(초기 질문)이 칸을 고른다
    td.append(value);
    if (typeof value === 'string') td.title = value;
    return td;
  }
  function table(columns, rows, onRow, sorting, lead) {
    // 숫자 열(값이 수인 열)은 오른쪽 정렬 — 렌더러가 원값으로 판정한다(열마다 표시를 따로 적지 않는다).
    // 시각(epoch 수)은 숫자가 아니라 날짜로 읽힌다 — 시각 칸 서식(when)은 제외.
    columns = columns.map((column) => ({ ...column, numeric: column.numeric ?? (column.format !== when && rows.some((row) => typeof row[column.key] === 'number')) }));
    const wrap = el('div', 'table-wrap');
    const grid = el('table');
    const head = el('thead');
    const titles = el('tr');
    const body = el('tbody');
    let current = { key: sorting?.key, dir: sorting?.dir };
    const headers = columns.map((column) => {
      const th = el('th');
      th.scope = 'col';
      th.dataset.key = column.key;  // 좁은 화면 카드형·패널 열림 시 숨길 칸을 CSS가 고른다
      if (column.numeric) th.className = 'num';
      // 정의(column.hint)는 머리 글자 옆 (i) — 정렬 버튼 밖에 둔다(누르면 정렬이 아니라 설명).
      const hint = column.hint ? [infoMark(column.hint)] : [];
      if (!sorting || (sorting.onSort && !column.sort)) { th.append(column.label, ...hint); return th; }
      const button = el('button', 'sort-button', column.label);
      button.type = 'button';
      button.onclick = () => {
        const dir = current.key === column.key && current.dir === 'asc' ? 'desc' : 'asc';
        if (sorting.onSort) { sorting.onSort(column.key, dir); return; }
        current = { key: column.key, dir };
        mark();
        fill(sortRows(rows, column.key, dir));
      };
      th.append(button, ...hint);
      return th;
    });
    const mark = () => columns.forEach((column, i) => {
      if (headers[i].firstElementChild) headers[i].setAttribute('aria-sort', current.key === column.key ? (current.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
    titles.append(...headers);
    head.append(titles);
    if (lead) { const tr = el('tr', 'lead-row'); for (const column of columns) tr.append(cellOf(column, lead[column.key])); head.append(tr); }
    mark();
    const fill = (list) => body.replaceChildren(...list.map((row, index) => {
      const tr = el('tr');
      // column.text(row)가 있으면 칸 글자는 행에서 만든다(정렬 키는 그대로 column.key — 서버 정렬과 무관).
      for (const column of columns) tr.append(cellOf(column, column.text ? column.text(row) : row[column.key]));
      if (onRow && (!onRow.enabled || onRow.enabled(row))) {
        tr.dataset.record = String(index);
        tr.tabIndex = 0;
        tr.setAttribute('aria-label', `${index + 1}번째 기록 상세 열기`);
        // 연 행은 패널이 열려 있는 동안 강조한다(대화 목록과 같은 aria-current) — 패널을 닫으면 푼다.
        const open = () => { for (const other of body.children) other.removeAttribute('aria-current'); tr.setAttribute('aria-current', 'true'); onRow(row); };
        tr.onclick = open;
        tr.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } };
      }
      return tr;
    }));
    fill(current.key && !sorting.onSort ? sortRows(rows, current.key, current.dir) : rows);
    grid.append(head, body);
    wrap.append(grid);
    return wrap;
  }

  function analysisCard(title, content, note) {
    const card = el('section', 'analysis-card');
    card.append(el('h3', null, title), content);
    if (note) card.append(el('p', 'analysis-note', note));
    return card;
  }

  const charts = initCharts({ template: $('chart-template'), el, table });
  const usd = (value) => value == null ? '—' : `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: value && Math.abs(value) < 1 ? 4 : 2 })}`;
  // 비율은 어디서나 소수 1자리(15.0% · +10.8%p) — 자릿수가 들쭉날쭉하면 비교가 어렵다.
  const percent = (value) => value == null ? '미측정' : `${value.toFixed(1)}%`;
  const points = (value) => `${value.toFixed(1)}%p`;
  // 답변거절률 = (실패 + 중단) ÷ 질의 — 통계 탭 '답변거절수'와 같은 정의(서버 refusals).
  const refusalRate = (s) => s?.turns ? s.refusals / s.turns * 100 : null;
  /** 역할 비교 — 서버 /me roles 순서(낮음 → 높음)가 정본. 화면 편의일 뿐 판정은 서버다. */
  const hasRole = (role) => state.roles.indexOf(state.role) >= state.roles.indexOf(role);

  /** 조회 기간의 모든 날짜(어드민 시간대 달력일) — API는 기록 있는 날만 주므로 빈 날을 여기서 채운다(추이에선 빈 날도 정보). */
  function periodDays(period) {
    const days = [];
    for (const day = new Date(`${period.since}T00:00:00Z`); day <= new Date(`${period.until}T00:00:00Z`); day.setUTCDate(day.getUTCDate() + 1)) days.push(day.toISOString().slice(0, 10));
    return days;
  }

  /** 증감 문구: 부호 + 절대 변화 (변화율). 비교할 이전 값이 있을 때만 부른다(kpi의 comparable). */
  function change(current, previous, format) {
    const difference = current - previous;
    const sign = difference > 0 ? '+' : difference < 0 ? '−' : '';
    // 비율 지표(%p)에 변화율을 또 붙이면 '퍼센트의 퍼센트'라 읽히지 않는다.
    if (format === points) return `${sign}${format(Math.abs(difference))}`;
    const rate = !difference ? '0%' : `${sign}${Math.abs(difference / previous * 100).toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%`;
    // 변화가 표시 자릿수 아래로 반올림되면('+$0.00') 절대값은 거짓 정보라 변화율만 남긴다.
    const absolute = format(Math.abs(difference));
    if (difference && absolute === format(0)) return rate;
    // 이전 값이 작으면(설정 admin_change_min_base) 비율이 과장된다(2 → 38 = +1,800%) — 절대 변화만.
    return Math.abs(previous) < state.changeMinBase ? `${sign}${absolute}` : `${sign}${absolute} (${rate})`;
  }

  /** KPI 타일. 증감 줄은 비교할 이전 값이 있을 때만 — 없거나(미측정) 0이면 변화율이 뜻이 없어 줄째 뺀다
   *  (비율 지표 %p는 이전 0%도 비교값이다). upIsBad면 증가를 빨강·감소를 초록으로 — 부호 글자가 함께 간다.
   *  open이 있으면 타일 전체가 버튼이다(그 숫자의 목록으로 간다 — title이 어디로 가는지 말한다). */
  /** sample(비율의 분모 — 두 기간 중 작은 쪽)이 설정 하한(change_min_base)보다 작으면 증감을 색 없이
   *  '표본 적음(n질의)'으로만 보인다 — 질의 3건 중 1건 실패가 '+33%p 악화'로 빨갛게 뜨지 않게. */
  function kpi(label, value, { current, previous, format, upIsBad, sub, open, sample, hint } = {}) {
    const box = el(open ? 'button' : 'div', 'kpi');
    if (open) { box.type = 'button'; box.onclick = open.go; box.title = open.title; }
    const name = el('span', null, label);
    if (hint) name.append(infoMark(hint));
    box.append(name, el('strong', null, value));
    if (sample != null && sample < state.changeMinBase) box.append(el('span', 'delta', `표본 적음(${num(sample)}질의)`));
    else if (format && current != null && previous != null && (previous !== 0 || format === points)) {
      const worse = upIsBad && current !== previous ? (current > previous ? ' bad' : ' good') : '';
      box.append(el('span', `delta${worse}`, `${change(current, previous, format)} · 이전 기간 대비`));
    }
    if (sub) box.append(el('span', 'kpi-sub', sub));
    return box;
  }

  /** 피드백 KPI — 좋아요·싫어요 각각을 누르면 이 기간 + 그 평가로 걸러 대화 탭을 연다.
   *  (대화 목록 기간은 세션 최근 갱신 기준이라 평가 시각 기준인 카드 수와 조금 다를 수 있다.) */
  function feedbackKpi(summary, prior, period) {
    const box = kpi('피드백 (좋아요 · 싫어요)', `${num(summary.likes)} · ${num(summary.dislikes)}`, { hint: METRIC_HINTS.feedback, sub: prior?.likes || prior?.dislikes ? `이전 기간 좋아요 ${num(prior.likes)} · 싫어요 ${num(prior.dislikes)}` : '' });
    const links = el('span', 'kpi-links');
    for (const rating of Object.keys(FEEDBACK_LABELS)) {
      const link = el('button', 'link-button', `${FEEDBACK_LABELS[rating]} 대화 보기`);
      link.type = 'button';
      link.disabled = !summary[rating === 'up' ? 'likes' : 'dislikes'];  // 0건이면 열 것이 없다
      link.onclick = () => openSessions({ since: period.since, until: period.until, rating });
      links.append(link);
    }
    box.append(links);
    return box;
  }

  function loadDashboard() {
    const params = new URLSearchParams({ since: $('dashboard-since').value, until: $('dashboard-until').value, ...scope() });
    showPeriod('dashboard');
    // 재조회는 이전 렌더를 흐리게 유지한다 — 자리표시자로 갈아 끼우면 화면이 튄다.
    if ($('dashboard').childElementCount) $('dashboard').classList.add('is-loading');
    else $('dashboard').replaceChildren(el('div', 'placeholder', '운영 지표를 불러오는 중…'));
    // 비용은 owner 전용 엔드포인트다 — 다른 역할은 부르지도 않는다(서버도 403).
    const cost = hasRole('owner') ? api(`/admin/api/analytics/cost?${params}`).catch((error) => error) : Promise.resolve(null);
    return request('dashboard', `/admin/api/analytics?${params}`, async (data) => renderDashboard(data, await cost))
      .finally(() => { if (!pending.has('dashboard')) $('dashboard').classList.remove('is-loading'); });
  }

  /** 대시보드(모든 역할): 기간 KPI 5개(이전 기간 대비) · 응답 시간 추이 · 시간대별 질의. owner면 비용 패널을 덧붙인다. */
  function renderDashboard(data, cost) {
    const summary = data.summary, prior = data.comparison?.summary;
    const days = periodDays(data.period);
    const byDay = new Map(data.daily.map((row) => [row.day, row]));
    const daily = (pick, missing) => days.map((day) => byDay.has(day) ? pick(byDay.get(day)) : missing);

    const { since, until } = data.period;
    const kpis = el('div', 'kpis');
    kpis.append(
      kpi(`${TERMS.users} (기간)`, metric(summary.users), { hint: METRIC_HINTS.users, current: summary.users, previous: prior?.users, format: num, sub: '이 기간에 대화한 사용자', open: { title: '회원 목록 보기', go: () => switchTab('users') } }),
      kpi(TERMS.turns, metric(summary.turns), { hint: METRIC_HINTS.queries, current: summary.turns, previous: prior?.turns, format: num, open: { title: '이 기간의 대화 보기', go: () => openSessions({ since, until }) } }),
      kpi('답변거절률', percent(refusalRate(summary)), { current: refusalRate(summary), previous: refusalRate(prior), format: points, upIsBad: true, sub: `${refusedText(summary.refused)} / ${num(summary.turns)}질의`,
        sample: Math.min(summary.turns ?? 0, prior?.turns ?? Infinity), hint: REFUSAL_HINT, open: { title: '이 기간의 거절(실패·중단) 대화 보기', go: () => openSessions({ since, until, status: 'refused' }) } }),
      feedbackKpi(summary, prior, data.period),
      kpi(TERMS.clicks, metric(summary.clicks), { hint: METRIC_HINTS.clicks, current: summary.clicks, previous: prior?.clicks, format: num, open: { title: '통계의 클릭 보기(같은 기간)', go: () => openStatistics('click') } }),
    );
    // 하루(오늘)면 선이 점 하나라 차트 대신 요약 수치. 여러 날이면 질의 없는 날은 선이 끊긴다(0초가 아니라 미측정).
    const latencyChart = days.length === 1 ? analysisCard('응답 시간', latencySummary(summary)) : charts.lines({
      title: '응답 시간', categories: days, format: seconds, onPick: (day) => openSessions({ since: day, until: day }),
      note: `기간 전체 보통 응답(p50) ${seconds(summary.p50_seconds)} · 느린 응답(p95) ${seconds(summary.p95_seconds)} · 질의가 없는 날은 선이 끊깁니다.`,
      series: [['p50_seconds', '보통 응답(p50)', 'series-1'], ['p95_seconds', '느린 응답(p95)', 'series-2']]
        .map(([key, label, className]) => ({ label, className, values: daily((row) => row[key], null) })),
    });
    // 시간대별 질의(기간 합계) — 요일 분포는 통계 탭이 맡는다. 기록 없는 시각은 0.
    const hours = [...Array(24).keys()], byHour = new Map(data.hourly.map((row) => [row.hour, row.turns]));
    const hourly = charts.stackedBars({
      title: `시간대별 ${TERMS.turns}`, categories: hours, unit: 'count', format: num,
      tick: (hour) => `${hour}시`, axisTitle: '시각',
      series: [{ label: TERMS.turns, className: 'series-1', values: hours.map((hour) => byHour.get(hour) ?? 0) }],
    });
    $('dashboard').replaceChildren(kpis, latencyChart, hourly, ...(cost ? costPanel(cost, days) : []));
  }

  /** 응답 시간 요약 수치(하루 기간) — p50 · p95 두 칸. */
  function latencySummary(summary) {
    const box = el('div', 'latency-today');
    for (const [label, value] of [['보통 응답(p50)', summary.p50_seconds], ['느린 응답(p95)', summary.p95_seconds]]) { const item = el('span', null, `${label} `); item.append(el('b', null, seconds(value))); box.append(item); }
    return box;
  }

  /** 비용 패널(owner) — 비용 KPI 2개 · 일별 비용 · 사용자별 비용 상위. 모델명은 서버가 싣지 않는다. */
  function costPanel(cost, days) {
    if (cost instanceof Error) return [analysisCard('비용', el('p', 'error', `비용을 불러오지 못했습니다: ${cost.message}`))];
    const summary = cost.summary, prior = cost.comparison?.summary;
    const { krw_per_usd: krw, krw_as_of: krwAsOf } = cost.currency;
    const kpis = el('div', 'kpis');
    kpis.append(
      kpi('비용 (USD 예상치)', usd(summary.cost_usd), { hint: METRIC_HINTS.cost, current: summary.cost_usd, previous: prior?.cost_usd, format: usd, sub: krw && summary.cost_usd != null ? `≈ ₩${num(Math.round(summary.cost_usd * krw))} · 환율 ${krwAsOf ?? '기준일 미기재'} 기준` : '' }),
      kpi('대화 질의당 비용', usd(summary.cost_per_turn_usd), { hint: METRIC_HINTS.cost_per_turn, current: summary.cost_per_turn_usd, previous: prior?.cost_per_turn_usd, format: usd, sub: `/ ${num(summary.priced_rows)} 질의` }),
    );
    const byDay = new Map(cost.daily.map((row) => [row.day, row]));
    // 일별 = 대화 + 대화 외(초기 질문 자동 생성처럼 사용자 귀속이 없는 작업 — 질의가 없는 날에도 비용이 있는 이유).
    // 그날 비용이 null(전부 단가 미등록)이면 0으로 그리되 표·툴팁은 막대 합으로 읽힌다.
    const part = (day, chat) => { const row = byDay.get(day); if (!row) return 0; const background = row.background_usd ?? 0; return chat ? (row.cost_usd ?? 0) - background : background; };
    const chart = days.length > 1 && charts.stackedBars({
      title: '일별 비용', categories: days, unit: 'usd', format: usd, onPick: (day) => openSessions({ since: day, until: day }),
      series: [{ label: '대화', className: 'series-1', values: days.map((day) => part(day, true)) }, { label: '대화 외', className: 'tone-grey', values: days.map((day) => part(day, false)) }],
    });
    const users = analysisCard('사용자별 비용 상위', cost.users.length ? table([
      {key: 'nickname', label: '닉네임', format: day}, {key: 'user_id', label: '회원번호'}, {key: 'priced_rows', label: TERMS.turns, format: metric}, {key: 'cost_usd', label: '대화 비용', format: usd}, {key: 'cost_per_turn_usd', label: '대화 질의당 비용', format: usd, hint: METRIC_HINTS.cost_per_turn},
    ], cost.users.map((row) => ({ ...row, nickname: row.nickname && row.nickname !== row.user_id ? row.nickname : null }))) : el('p', 'analysis-note', '사용자의 대화 비용 기록이 없습니다.'), '사용자의 대화 비용만 셉니다(대화 외 작업은 빠집니다).');
    const head = el('div', 'section-head');
    head.append(el('h3', null, '비용'), el('span', null, `${roleLabel('owner')} 전용 · 예상치`));
    // 각주 = 예상치 안내 · '대화 외' 정의(서버 cost_notes) — 산식 상세는 운영 문서(docs/admin-operations.md).
    return [head, kpis, el('p', 'analysis-note cost-note', cost.notes.join(' ')), ...(chart ? [chart] : []), users];
  }

  /** 기록 표 — 라벨 · 값 두 열. columns가 있으면 그 순서·라벨·시각 서식, 없으면 키 이름 그대로. */
  const labelWithHint = (label, hint) => { const node = el('span', null, label); node.append(infoMark(hint)); return node; };
  function showRecord(record, columns = state.recordColumns) {
    state.recordColumns = columns;
    const rows = (columns || Object.keys(record || {}).map((key) => ({ key, label: key })))
      .map((column) => ({ label: column.hint ? labelWithHint(column.label, column.hint) : column.label, value: column.text ? column.text(record) : column.format ? column.format(record[column.key]) : valueText(record[column.key]) }));
    // 라벨·값 두 열 — 값 열은 필드마다 종류가 달라 숫자 정렬을 하지 않는다.
    const grid = record && table([{ key: 'label', label: '항목', numeric: false }, { key: 'value', label: '값', numeric: false }], rows);
    grid?.querySelector('thead').remove();  // 라벨 · 값 두 열이라 머리행('항목 · 값')은 정보가 없다
    $('record-view').replaceChildren(...(grid ? [grid] : []));
  }

  /** 옆 패널(비모달) — 회원·초기 질문·관리자 행과 관리 폼이 같은 패널을 쓴다. 폼은 #record-actions에 붙는다.
   *  비모달이라 패널을 연 채 다른 행을 누르면 내용만 바뀐다. */
  async function openDialog(title, record, columns = null) {
    if (panelDirty() && !(await confirmLeave())) return false;
    $('record-title').textContent = title;
    showRecord(record, columns);
    $('record-actions').replaceChildren();
    $('record-dialog').show();
    return true;
  }
  /** 옆 패널을 떠나도 되는지 — 폼에 저장하지 않은 변경(form.dirty)이 있을 때만 묻는다(다른 행·Esc·×·화면 이동 공통).
   *  깨끗하면 기다리지 않는다(await에 닿지 않아 호출부의 순서가 그대로다). */
  const panelDirty = () => $('record-dialog').open && [...$('record-actions').querySelectorAll('form')].some((form) => form.dirty?.());
  const confirmLeave = () => confirmDialog({ title: '저장하지 않은 변경이 있습니다', message: '이 패널을 떠나면 입력한 내용이 사라집니다.', confirmLabel: '버리고 나가기', danger: true });
  async function closePanel() {
    if (panelDirty() && !(await confirmLeave())) return false;
    $('record-dialog').close();
    return true;
  }

  /** 확인 대화상자(모달) — confirm() 대신. 결과는 Promise<boolean>(취소·Esc는 false). 위험한 동작은 취소에 먼저 포커스. */
  function confirmDialog({ title, message, confirmLabel = '확인', danger = false }) {
    const dialog = $('confirm-dialog'), ok = $('confirm-ok');
    $('confirm-title').textContent = title;
    $('confirm-message').textContent = message;
    ok.textContent = confirmLabel;
    ok.classList.toggle('danger', danger);
    dialog.showModal();
    (danger ? $('confirm-cancel') : ok).focus();
    return new Promise((resolve) => {
      const done = (value) => { dialog.close(); resolve(value); };
      ok.onclick = () => done(true);
      $('confirm-cancel').onclick = () => done(false);
      dialog.oncancel = (event) => { event.preventDefault(); done(false); };
    });
  }
  /** 알림(토스트) — 끝난 동작 한 줄 + (있으면) 되돌리기. TOAST_MS 뒤 사라진다. */
  const TOAST_MS = 5000;  // 되돌리기를 누를 시간(화면 동작 상수)
  let toastTimer = null;
  function showToast(message, action) {
    clearTimeout(toastTimer);
    $('toast-message').textContent = message;
    $('toast-action').hidden = !action;
    if (action) { $('toast-action').textContent = action.label; $('toast-action').onclick = () => { $('toast').hidden = true; action.run(); }; }
    $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, TOAST_MS);
  }

  // ── 회원 · 초기 질문: 목록(검색·페이지) + 옆 패널 편집. 편집 폼은 admin_manage.js가 만든다.
  /** 시각 칸 — 대화 목록과 같은 모양(날짜 + 작은 시각). */
  const when = (ts) => {
    if (ts == null) return '-';
    const [date, time] = clockParts(ts), cell = el('span', 'stamp', date);
    cell.append(el('small', null, time));
    return cell;
  };
  const day = (value) => value ?? '-';
  const flag = (on, yes, no = '-') => (on ? yes : no);
  const SOURCE_KINDS = { auto: '자동 생성', manual: '직접 등록' };
  /** 고정 종류(서버 starters.PIN_NAMES의 이름) — 이름·한 줄 설명·순위 한 곳. 첫 화면 고정과 분야 고정은
   *  같은 아이콘을 쓰지 않고 이 이름(배지)으로 구분한다. */
  const PINS = {
    '': { label: '고정 안 함', rank: 0 },
    // 이름에 조건을 담는다 — 분야 대표는 그 분야가 뽑힌 날에만, 첫 화면 고정은 날마다(둘 다 '항상'이라 쓰지 않는다).
    slot: { label: '분야 대표 고정', rank: 1, help: '분야가 뽑힌 날에만 그 분야 대표로 나와요' },
    global: { label: '첫 화면 고정', rank: 2, help: '날마다 첫 화면에 반드시 나와요' },
  };
  const pinRank = (row) => PINS[row.pinned]?.rank ?? 0;  // 모르는 값(재기동 전 옛 응답)은 고정 아님
  const pinBadge = (kind) => (PINS[kind]?.rank ? el('em', `pin-badge pin-${kind}`, PINS[kind].label) : null);
  /** RBTI 배지 버튼 — 누르면 그 유형으로 거른다(행 클릭과 겹치지 않게 전파를 막는다). */
  function rbtiButton(name, code) {
    const button = el('button', 'rbti', code);
    button.type = 'button';
    button.title = `${rbtiLabel(code)}만 보기 · 다시 누르면 해제`;
    button.onclick = (event) => { event.stopPropagation(); toggleRbti(name, code); };
    return button;
  }
  const USER_COLUMNS = [
    { key: 'nickname', label: '닉네임', format: day, sort: true }, { key: 'user_no', label: '회원번호', sort: true }, { key: 'rbti', label: 'RBTI', format: (code) => (code ? rbtiButton('users', code) : '-'), sort: true },
    { key: 'turns', label: `${TERMS.turns} (누적)`, format: num, sort: true, hint: METRIC_HINTS.member_turns },
    // users.created_at = 이 회원의 첫 API 키 인증 때 만든 행(auth.py _register) — '가입'이 아니라 첫 인증 시각.
    { key: 'last_chat_at', label: '마지막 질의', format: when, sort: true }, { key: 'created_at', label: '첫 이용일', format: when, sort: true },
  ];
  // 옆 패널 — 회원번호에 복사 버튼.
  const USER_DETAIL = USER_COLUMNS.map((column) => (column.key !== 'user_no' ? column : { ...column, format: (value) => {
    const cell = el('span', null, value);
    cell.append(' ', copyButton(value));
    return cell;
  } }));
  // 슬롯은 내부 키 대신 칩 라벨(서버가 서빙과 같은 규칙으로 채움). 노출 기간·사용(on/off)은 편집 패널에만.
  // 지금 노출 = 서빙 풀과 같은 판정(서버 live_predicate), 사용 수 = 최근 N일 새 대화 중 이 문장으로 시작한 수.
  /** 확률의 뜻 — (i) 툴팁. */
  const PROBABILITY_HINT = '첫 화면은 분야 몇 개를 무작위로 고르고, 분야마다 질문 하나를 보여 줘요. 이 숫자는 그날 첫 화면에 이 질문이 들어갈 확률이에요.';
  const STARTER_COLUMNS = [
    { key: 'label', label: '분야', sort: true }, { key: 'text', label: '문장' }, { key: 'source', label: '등록 방식', format: (v) => SOURCE_KINDS[v] ?? v },
    // 자동 = 생성 실행일, 직접 등록 = 등록 시각의 날짜(서버 정렬도 같은 값).
    { key: 'run_date', label: '생성·등록일', sort: true, text: (row) => row.run_date ?? (row.created_at ? stamp(row.created_at).slice(0, 10) : '-') },
    { key: 'pinned', label: '고정', format: (v) => pinBadge(v) ?? '-', sort: true }, { key: 'live', label: '지금 노출', sort: true, text: (row) => (row.live ? '노출 중' : notLiveReason(row.not_live_reason)) },
    { key: 'probability', label: '첫 화면 확률', hint: PROBABILITY_HINT, text: (row) => listProbability(row) },
    { key: 'uses', label: '사용 수', format: (v) => (typeof v === 'number' ? num(v) : v), sort: true, hint: METRIC_HINTS.uses },
  ];
  /** 같은 문장 행이 여럿이면 사용 수는 문장 단위라 함께 본다 — 센 것이 있을 때만 알린다(0에 붙이면 헷갈린다). */
  const starterRow = (row) => (row.uses_shared && row.uses ? { ...row, uses: `${num(row.uses)} (같은 문장 공유)` } : row);
  /** 노출 안 됨의 이유(서버 not_live_reason) → 운영자 문구. 키는 starters.live_clauses 조건 + 꺼진 행의 주체
   *  (replaced = 자동 생성이 새 질문으로 바꿈, stopped = 운영자가 끔 — admin._split_inactive). */
  const notLiveReason = (key) => ({
    replaced: '새로 생성된 질문으로 교체됨', stopped: '운영자가 노출을 끔', out_of_window: '노출 기간 밖',
    stale_auto: `오래된 자동 생성 질문(생성 ${num(state.starterSummary?.pool_days ?? 0)}일 경과)`, not_today: '오늘 생성분이 아님(화제·무엇이든은 당일 생성분만)',
  })[key] ?? (key ? key : '-');
  const percentText = (p) => (p == null ? '-' : `${(p * 100).toFixed(1)}%`);
  /** 첫 화면 확률 글자 — 소수 한 자리(확률은 날짜 시드라 흔들리지 않는다), 1% 미만은 '<1%'. */
  const chanceText = (p) => (p >= 1 ? '100%' : p > 0 && p < 0.01 ? '<1%' : percentText(p));
  /** 분야 하나가 그날 첫 화면에 뽑힐 확률 — 첫 화면 고정이 먼저 차지한 칸·분야를 뺀 남은 칸 ÷ 남은 분야
   *  (날짜 패널 머리말·고정 예상이 같은 값). added = 그 풀에 아직 없는 새 분야가 하나 더해질 때 1. */
  const slotProbability = (data, added = 0) => {
    const pins = data?.global_pins ?? { used: 0, slots: 0 }, slots = (data?.slot_count ?? 0) - pins.slots + added;
    return slots > 0 ? Math.min(1, Math.max(0, data.n - pins.used) / slots) : null;
  };
  /** 확률이 0인 노출 중 질문의 이유(서버 preview zero_reason — 고정에 가려짐·실제 중복 건너뜀·표본에 안 나옴). 0% 대신 이것. */
  const ZERO_REASONS = {
    pinned_sibling: '같은 분야의 고정 질문(분야 대표·첫 화면)이 대신 나와요',
    duplicate: '같은 책을 가리키는 다른 질문과 겹쳐 빠져요',
    no_room: '첫 화면 고정이 칸을 모두 차지해요',
  };
  /** 확률 칸 글자 — 0보다 크면 %, 0이면 이유. row = preview 행({probability, zero_reason}). */
  const probabilityText = (row) => (row.probability > 0 ? chanceText(row.probability) : ZERO_REASONS[row.zero_reason] ?? '-');
  /** 목록 칸의 확률 — 오늘 미리보기(state.preview)의 그 행 값. 0이면 이유 약자 + 툴팁(전체 이유), 노출 안 됨은 '-'. */
  const ZERO_SHORT = { pinned_sibling: '고정에 가려짐', duplicate: '중복 제외', no_room: '칸 없음' };
  function listProbability(row) {
    const hit = row.live && state.preview?.rows.find((item) => item.id === row.id);
    if (!hit) return '-';
    if (hit.probability > 0) return chanceText(hit.probability);
    const cell = el('span', 'dim', ZERO_SHORT[hit.zero_reason] ?? '-');
    cell.title = probabilityText(hit);
    return cell;
  }
  // 패널엔 슬롯 내부 키를 싣지 않는다 — 운영자에겐 칩 라벨('슬롯')이 이름이다. 확률은 미리보기(날짜·개수)의 행별 값.
  // 행 패널 읽기 표 — 맨 위 상태 요약(분야·지금 노출·확률·고정)이 말하는 칸은 뺀다(같은 말을 두 번 하지 않는다).
  const SUMMARY_KEYS = ['label', 'live', 'probability', 'pinned'];
  const starterDetail = () => [...STARTER_COLUMNS.filter((column) => !SUMMARY_KEYS.includes(column.key)), { key: 'active', label: '노출 설정', format: (v) => flag(v, '켜짐', '꺼짐') },
    { key: 'valid_from', label: '노출 시작일', format: day }, { key: 'valid_until', label: '노출 종료일', format: day },
  ];
  // 편집 폼이 있으면(운영자 이상) 폼이 다루는 문장·우선 노출·사용·기간은 표에서 빼고 읽기 전용만 남긴다.
  const STARTER_READONLY_KEYS = ['source', 'run_date', 'uses'];

  /** 목록 한 화면 — 표 + 건수 + crema 페이지 버튼. key는 pane 접두(users·starters), 페이지는 state[key + 'Page']. */
  /** prepare: 받은 행을 표에 넣기 전에 화면용 필드를 덧붙인다(감사 기록의 사람이 읽는 요약 등).
   *  onData: 응답 전체로 할 일(감사 기록의 계정 선택지 채우기 등). */
  function loadList(key, path, columns, onRow, prepare = (row) => row, onData = () => {}) {
    writeHash();
    const page = state[key + 'Page'], sort = state.sort[key];
    const params = new URLSearchParams({ page, ...(sort ? { sort: sort.key, dir: sort.dir } : {}) });
    $(key).replaceChildren(el('div', 'placeholder', '불러오는 중…'));
    const base = path.replace(/\?$/, '');  // 조건이 없으면 경로가 '?'로 끝난다 — '?&page='가 되지 않게
    return request(key, `${base}${base.includes('?') ? '&' : '?'}${params}`, (data) => {
      $(key + '-info').textContent = rangeText(data);
      onData(data);
      // 서버 정렬 — 열을 누르면 첫 페이지부터 그 순서로 다시 읽는다(검색 조건은 그대로).
      const sorting = { ...sort, onSort: (sortKey, dir) => { state.sort[key] = { key: sortKey, dir }; state[key + 'Page'] = 0; loaders[key](); } };
      $(key).replaceChildren(data.items.length ? table(columns, data.items.map(prepare), onRow, sorting) : emptyList(key, '조건에 맞는 기록이 없습니다.'));
      cremaPager($(key + '-pager'), data.total, data.page_size, page, (to) => { state[key + 'Page'] = to; loaders[key](); });
    });
  }
  const loadUsers = () => loadList('users', `/admin/api/users?${new URLSearchParams({ ...state.applied.users, ...scope() })}`, USER_COLUMNS, openUser, undefined, (data) => {
    addMissingOptions($('users-rbti'), data.rbti_types, rbtiLabel);
    // 대화 상세의 '회원 보기'로 왔으면 그 회원 패널을 연다(회원번호 정확 일치 검색의 결과 행).
    const wanted = state.pendingMember, hit = data.items.find((row) => String(row.user_no) === wanted);
    state.pendingMember = null;
    if (hit) openUser(hit);
  });
  const loadStarters = () => {
    for (const view of ['calendar', 'list', 'popular']) $(`starters-${view}`).hidden = state.startersView !== view;
    if (state.startersView === 'popular') return loadPopular();
    if (state.startersView === 'calendar') return loadCalendar();
    manage.starterTools($('starters-tools'));
    // '첫 화면 확률' 칸·행 패널·고정 경고는 오늘 미리보기(state.preview)를 쓴다 — 없거나 쓰기 뒤면 먼저 받는다.
    // '지금 노출 중'만 보는 중이면 '지금 노출' 칸은 모두 같은 값이라 뺀다.
    const columns = state.applied.starters?.status === 'live' ? STARTER_COLUMNS.filter((column) => column.key !== 'live') : STARTER_COLUMNS;
    return todayPreview().then(() => loadList('starters', `/admin/api/starters?${new URLSearchParams({ ...state.applied.starters, ...scope() })}`, columns, openStarter, starterRow, (data) => {
      state.starterSlots = data.slots;
      state.starterSummary = data.summary;
      // 슬롯 선택지 = 칩 라벨(값은 슬롯 키) — 주소가 먼저 넣은 키 선택지는 글자만 라벨로 바꾼다.
      const options = new Map([...$('starters-slot').options].map((option) => [option.value, option]));
      for (const { slot, label } of data.slots) options.has(slot) ? (options.get(slot).textContent = label) : $('starters-slot').append(new Option(label, slot));
      queryNote('starters');  // 조건 칩도 슬롯 키 대신 라벨로
      syncMoreFilters();
      // 감사 기록의 '이 질문 보기'로 왔으면 그 행 패널을 바로 연다(id로 거른 목록의 한 행).
      const wanted = state.pendingStarter, hit = data.items.find((row) => String(row.id) === wanted);
      state.pendingStarter = null;
      if (hit) openStarter(hit);
    }));
  };
  /** 오늘 미리보기 한 벌(시드 고정) — 쓰기(고정·노출·등록)가 state.preview를 비우면 다음 읽기에서 다시 받는다. 실패는 확률 칸 '-'. */
  const todayPreview = () => (state.preview ? Promise.resolve() : api('/admin/api/starters/preview?seed=1').then((data) => { state.preview = data; }, () => {}));

  // ── 첫 화면 미리보기(목록 위 · 날짜 패널 공용): 서버가 서빙과 같은 풀·선택으로 계산한다(serve·생성
  // 없음). 같은 시드면 같은 결과라 다시 읽어도 칩이 그대로이고 '다시 뽑기'만 새 시드를 쓴다.
  // mode: exact(오늘) · estimated(미래 — 자동은 오늘 풀로 가정) · record(과거 — 생성 기록, 칩 없음).
  const newSeed = () => Math.floor(Math.random() * 2 ** 31);
  const PREVIEW_NOTES = {
    exact: '지금 노출 중인 질문 기준 — 실제 첫 화면과 같은 기준입니다.',
    estimated: '예상치 — 직접 등록 질문은 이 날 기준, 자동 생성 질문은 오늘 노출 중인 것으로 가정했습니다.',
    record: '생성 기록 — 이 날 만들어진 자동 질문입니다. 그날 실제로 노출된 것을 재현한 것은 아닙니다.',
  };
  const poolCount = (data) => `분야 ${num(data.slot_count)}개 · ${data.mode === 'record' ? '자동 생성' : '질문'} ${num(data.pool_size)}개`;
  /** 날짜 패널의 첫 화면 예시 — 한 줄(분야 이름들)로 접어 두고 펼치면 칩 · 다시 뽑기 · 안내. 질문 수는 서버가
   *  실제 첫 화면과 같게 정한다. key는 request 중복 취소 키(오류 문구도 이 자리에), onData(응답)는 받을 때마다. */
  function previewView(date, key, onData) {
    const wrap = el('div', 'preview-view'), fold = el('details', 'preview-fold'), summary = el('summary'), head = el('div', 'preview-head');
    const chips = el('div', 'preview-chips'), note = el('p', 'analysis-note'), redraw = el('button', 'crema-button', '다른 예시 보기');
    redraw.type = 'button';
    head.append(redraw);
    const caption = el('p', 'preview-caption');
    fold.append(summary, head, caption, chips, note);
    wrap.id = key;  // request()가 오류 문구·다시 시도를 여기에 그린다
    let seed = newSeed();
    const load = () => request(key, `/admin/api/starters/preview?${new URLSearchParams({ date, seed })}`, (data) => {
      const record = data.mode === 'record';
      wrap.replaceChildren(fold);  // 오류 뒤 다시 시도면 자리를 되찾는다
      summary.textContent = record ? '생성 기록 안내' : `첫 화면 예시 · ${data.sample.map((chip) => chip.label).join(' · ') || '없음'}`;
      head.hidden = record;
      chips.replaceChildren(...data.sample.map((chip) => {
        const node = el('span', 'preview-chip');
        node.append(el('small', null, chip.label), ...(chip.pinned === 'global' ? [' ', pinBadge('global')] : []), el('span', null, chip.text));
        node.title = chip.text;
        return node;
      }));
      note.textContent = PREVIEW_NOTES[data.mode];
      // 칩은 매번 무작위로 뽑은 예시 한 벌이다 — 언제 뽑았는지 함께(다른 예시 보기를 누르면 바뀐다). 확률은 날짜로 고정.
      caption.textContent = record ? '' : `예시 · 무작위 · ${stamp(Date.now() / 1000).slice(11)} 기준`;
      onData(data);
    });
    redraw.onclick = () => { seed = newSeed(); load(); };
    load();
    return wrap;
  }

  /** 인기 질문 — 칩이 아닌 첫 질문 중 자주 나온 것(서버가 정규화로 묶어 센다). 운영자 이상은 행의
   *  '칩으로 추가'로 수동 추가 폼을 문장 채운 채 연다 — 사용자가 직접 친 문장이라 개인정보를 확인하게 한다. */
  function loadPopular() {
    // 기간 선택지·기본 기간은 서버 설정(day_options · starter_uses_days) — 고르기 전엔 서버 기본.
    const days = $('popular-days').querySelector('[aria-pressed="true"]')?.dataset.days;
    return request('popular', `/admin/api/starters/popular?${new URLSearchParams({ ...(days ? { days } : {}), ...scope() })}`, (data) => {
      $('popular-days').replaceChildren(...data.day_options.map((option) => {
        const button = el('button', null, `최근 ${num(option)}일`);
        button.type = 'button';
        button.dataset.days = String(option);
        button.setAttribute('aria-pressed', String(option === data.days));
        button.onclick = () => { press($('popular-days'), button); loadPopular(); };
        return button;
      }));
      state.starterSlots = data.slots;
      const s = data.summary;
      $('popular-note').textContent = `최근 ${num(data.days)}일 새 대화 ${num(s.sessions)}건 중 ${num(s.chip_sessions)}건(${s.sessions ? percentText(s.chip_sessions / s.sessions) : '-'})이 첫 화면 질문으로 시작했어요. 아래는 첫 화면 질문이 아닌 첫 질문을 많은 순으로 보입니다.`;
      const editor = hasRole('editor');
      const columns = [
        { key: 'text', label: '첫 질문' }, { key: 'sessions', label: '새 대화 수', format: num }, { key: 'last_at', label: '마지막', format: when },
        ...(editor ? [{ key: 'add', label: '등록' }] : []),  // 머리 글자는 화면에서 숨긴다(.sr-head) — 칸 이름은 읽힌다
      ];
      const rows = data.items.map((item) => {
        // 설정 최소 글자 미만("안녕" 등)은 시험 입력일 가능성이 커서 흐리게 + '짧음', 칩 상한을 넘으면 '줄여야 등록' 배지.
        const short = item.text.length < data.min_chars, text = el('span', short ? 'dim' : null, item.text);
        if (short) { text.title = `${num(data.min_chars)}자 미만 — 시험 입력일 수 있습니다`; text.prepend(el('em', 'short-tag', '짧음')); }
        if (!item.fits) text.append(Object.assign(el('em', 'short-tag long', `${num(item.text.length)}자 · 줄여야 등록`), { title: `첫 화면 질문은 ${num(data.max_chars)}자까지입니다` }));
        // 등록은 행을 누르면 열린다 — 이 칸은 넓은 화면에서 가리키거나 포커스한 행에만 보이는 안내다(50줄 반복 링크 대신).
        return { ...item, raw: item.text, short, text, add: short ? '' : el('span', 'row-action', '등록 →') };
      });
      const open = (row) => manage.openStarterCreate({ text: row.raw });
      open.enabled = (row) => editor && !row.short;
      const shown = rows.slice(0, state.popularShown);
      const more = el('button', 'crema-button more-rows', `더 보기 (${num(rows.length - shown.length)}개)`);
      more.type = 'button';
      more.onclick = () => { state.popularShown += POPULAR_PAGE; loadPopular(); };
      $('popular').replaceChildren(...(rows.length ? [table(columns, shown, open), ...(rows.length > shown.length ? [more] : [])] : [placeholder('이 기간에 두 번 이상 나온, 첫 화면 질문이 아닌 첫 질문이 없습니다.')]));
    });
  }
  const POPULAR_PAGE = 20;  // 인기 질문을 한 번에 보이는 줄 수(화면 배치 상수)

  // ── 캘린더: 월 격자(좁은 화면은 주 단위 목록). 날짜 기준은 서버 today — 브라우저 날짜를 쓰지 않는다.
  // 칸: 오늘 = 지금 노출(수 · 슬롯), 과거 = 그날 생성된 자동 질문 수(기록), 미래 = 막대만.
  // 기간 없는 직접 등록은 막대가 없어 위 안내 줄(cal-note)이 수와 분야를 말한다.
  // 막대: 기간을 정한 활성 수동 질문(고정은 진한 색 + 핀). 기간 없는 고정은 위 '상시 고정' 띠.
  const LANES = 3;  // 칸당 막대 줄 수 — 넘치면 '+k'
  const WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일'];
  const narrow = matchMedia('(max-width: 720px)');
  narrow.addEventListener('change', () => { if (state.calendar && state.startersView === 'calendar') renderCalendar(); });
  const monthText = (month) => `${month.slice(0, 4)}년 ${Number(month.slice(5))}월`;
  const addDays = (iso, n) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * 864e5).toISOString().slice(0, 10);
  const weekdayOf = (iso) => (new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7;  // 월 = 0

  /** 달력 위 '지금 첫 화면' — 오늘 미리보기 한 벌(칩) + 다른 예시 보기 + 첫 화면 고정 사용 수.
   *  확률은 날짜 시드라 시드와 무관해 이 응답이 목록 확률 칸·고정 경고의 state.preview도 된다.
   *  첫 화면 고정은 늘 들어가므로 따로 줄을 두지 않고 칩의 배지로 보인다(옛 '고정 · 기간 없음' 줄은
   *  분야 고정을 전역처럼 보여 헷갈렸다). */
  function loadNow(seed = newSeed()) {
    return request('cal-now', `/admin/api/starters/preview?seed=${seed}`, (data) => {
      state.preview = data;
      const box = $('cal-now'), chips = el('div', 'now-chips'), redraw = el('button', 'link-button', '다른 예시 보기');
      redraw.type = 'button';
      redraw.onclick = () => loadNow();
      chips.append(...data.sample.map((chip) => {
        const node = el('span', `now-chip${chip.pinned === 'global' ? ' pin-global' : ''}`);
        node.append(...(chip.pinned === 'global' ? [pinBadge('global')] : []), el('small', null, chip.label), el('span', null, chip.text));
        node.title = `${chip.pinned === 'global' ? '첫 화면 고정 · ' : ''}${chip.label} · ${chip.text}`;
        return node;
      }));
      const { count = 0, limit = 0 } = data.global_pins ?? {}, head = el('div', 'now-head');
      // 사용 수 = 등록 검사(422)와 같은 셈(끝나지 않은 첫 화면 고정) — 상한을 넘으면(설정을 낮춘 경우 등) 서빙이 먼저 등록한 것부터 자른다.
      head.append(el('b', null, '지금 첫 화면'), el('span', 'now-meta', `첫 화면 고정 ${num(count)}/${num(limit)}개`), redraw);
      box.replaceChildren(head, chips, ...(count > limit ? [el('p', 'notice-warn', `첫 화면 고정이 ${num(count)}개라 상한 ${num(limit)}개를 넘었어요 — 먼저 등록한 ${num(limit)}개만 나와요.`)] : []));
    });
  }
  function loadCalendar() {
    return request('calendar', `/admin/api/starters/calendar${state.calMonth ? `?month=${state.calMonth}` : ''}`, (data) => {
      state.calendar = data;
      state.calMonth = data.month;
      state.starterSlots = data.slots;
      writeHash();
      renderCalendar();
      loadNow();
    });
  }
  /** 막대 조각 — 한 주(또는 한 줄) 안에서 [시작, 끝] 날짜로 잘린 기간. 열린 끝은 멀리 둔다. */
  const span = (item) => [item.valid_from ?? '0000-01-01', item.valid_until ?? '9999-12-31'];
  /** 한 주의 막대 줄 배정 — 시작이 이른 것부터 비어 있는 첫 줄(모든 칸이 같은 줄 번호를 써서 막대가 이어진다). */
  function lanesFor(weekStart, weekEnd) {
    const lanes = [], placed = [];
    const ordered = [...state.calendar.schedules].sort((a, b) => span(a)[0].localeCompare(span(b)[0]) || a.id - b.id);
    for (const item of ordered) {
      const [from, until] = span(item);
      if (until < weekStart || from > weekEnd) continue;
      const start = from < weekStart ? weekStart : from, end = until > weekEnd ? weekEnd : until;
      let lane = lanes.findIndex((last) => last < start);
      if (lane < 0) { lane = lanes.length; lanes.push(end); } else lanes[lane] = end;
      placed.push({ item, lane, start, end });
    }
    return placed;
  }
  /** 막대 글자 — (핀) 라벨 · 문장, 끝을 넘으면 → (좁은 화면은 '~종료일'). */
  function barLabel(item, compact) {
    const text = el('span', 'cal-bar-text');
    if (PINS[item.pinned]?.rank) text.append(el('b', `bar-tag pin-${item.pinned}`, item.pinned === 'global' ? '첫 화면' : '분야 대표'));
    text.append(`${item.label} · ${item.text}`);
    if (compact) text.append(` ${item.valid_until ? `~${item.valid_until.slice(5)}` : '~계속'}`);
    return text;
  }
  const barTitle = (item) => `${PINS[item.pinned]?.rank ? `${PINS[item.pinned].label} · ` : ''}${item.label} · ${item.text} (${item.valid_from ?? '처음부터'} ~ ${item.valid_until ?? '계속'})`;
  /** 칸 요약 글자 — 날짜 종류마다 뜻이 다르다(패널 머리가 자세히 설명한다). */
  function daySummary(cell) {
    const box = el('span', 'cal-sum');
    // 오늘 = 지금 노출 중인 질문 수(분야 수는 툴팁) + 오늘 자동 생성 수, 과거 = 그날 자동 생성 수
    // (0은 비운다 — 노이즈), 미래 = 막대만. 생성 수의 정의(툴팁)는 오늘·과거·앞뒤 달 칸이 같다.
    const made = `이날 자동으로 만든 질문 ${num(cell.generated)}개`;
    if (cell.kind === 'today') {
      box.append(el('b', null, `노출 중 ${num(cell.live)}`), ...(cell.generated ? [`오늘 자동 ${num(cell.generated)}`] : []));
      box.title = [`지금 첫 화면에 나올 수 있는 질문 ${num(cell.live)}개 · 분야 ${num(cell.slots)}개`,
        ...(cell.generated ? [`${made} — 오늘 생성이 진행 중이면 늘어날 수 있어요.`] : [])].join('\n');
    } else if (cell.generated) { box.append(`자동 ${num(cell.generated)}`); box.title = made; }
    return box;
  }
  const inRange = (iso) => state.range?.end && iso >= state.range.start && iso <= state.range.end;

  function renderCalendar() {
    const data = state.calendar, editor = hasRole('editor');
    $('cal-month').textContent = monthText(data.month);
    // 상단 안내 한 문단 — 기간 없는 직접 등록(막대가 없어 달력에 안 보인다)은 늘, 기간 질문 0개 안내는 그때만.
    const { count, slots } = data.ongoing, note = [];
    if (count) {
      const open = el('button', 'link-button', `기간 없이 매일 나오는 직접 등록 질문 ${num(count)}개 · ${slots.map((slot) => slot.label).join(', ')}`);
      open.title = '목록에서 보기(직접 등록 · 기간 없음 · 지금 노출 중)';
      open.onclick = () => { pushHistory(); setView('starters', 'list'); openList('starters', { source: 'manual', term: 'none', status: 'live' }); };
      note.push(open);
    }
    if (!data.schedules.length) note.push(`${count ? ' — ' : ''}노출 기간을 정한 질문은 ${count ? '아직 ' : ''}없어요.`);
    $('cal-note').replaceChildren(...note);
    $('cal-note').hidden = !note.length;
    syncRange();
    const cells = new Map(data.days.map((cell) => [cell.date, cell])), outside = new Map(data.outside.map((cell) => [cell.date, cell.generated]));
    const first = data.days[0].date, last = data.days.at(-1).date;
    const weeks = [];
    for (let start = addDays(first, -weekdayOf(first)); start <= last; start = addDays(start, 7)) weeks.push(start);
    if (narrow.matches) { $('calendar').replaceChildren(agenda(weeks, cells)); return; }
    const grid = el('div', 'cal-grid');
    grid.setAttribute('role', 'grid');
    grid.append(...WEEKDAYS.map((name) => el('span', 'cal-weekday', name)));
    for (const weekStart of weeks) {
      const placed = lanesFor(weekStart, addDays(weekStart, 6));
      for (let i = 0; i < 7; i++) {
        const iso = addDays(weekStart, i), cell = cells.get(iso);
        // 이 달 밖 날짜는 흐린 날짜만(수치는 그 달 화면 몫) — 막대는 그 칸까지 잇는다.
        const box = cell ? dayButton(cell, 'cal-cell') : el('div', 'cal-cell outside');
        // 앞뒤 달 칸도 생성 기록 수는 보인다(흐린 칸, 누르지 않음 — 그 달 화면에서 연다).
        if (!cell) box.append(el('span', 'cal-date', iso.slice(5).replace('-', '/')), daySummary({ kind: 'past', generated: outside.get(iso) }));
        const here = placed.filter((bar) => bar.start <= iso && bar.end >= iso);
        for (let lane = 0; lane < LANES; lane++) {
          const bar = here.find((b) => b.lane === lane), line = el('span', bar ? `cal-bar${PINS[bar.item.pinned]?.rank ? ` pinned pin-${bar.item.pinned}` : ''}` : 'cal-bar empty');
          if (bar) {
            // 글자는 주마다 조각의 첫 칸에 반복. 끝 모양이 기간을 말한다 — 실제 시작·종료 칸만 둥글고, 이어지면 각지다.
            if (iso === bar.start) line.append(barLabel(bar.item));
            line.title = barTitle(bar.item);
            line.classList.toggle('start', iso === span(bar.item)[0]);
            line.classList.toggle('end', iso === span(bar.item)[1]);
            line.onclick = (event) => { event.stopPropagation(); openSchedule(bar.item); };
            if (bar.item.id === state.flashId) line.classList.add('flash');  // 방금 등록한 질문
          }
          box.append(line);
        }
        const hidden = here.filter((b) => b.lane >= LANES).length;
        // 칸에 다 못 그린 막대 수 — 칸(누르면 그날 패널)과 함께 눌린다.
        if (hidden) box.append(Object.assign(el('span', 'cal-overflow', `+${num(hidden)}개 더`), { title: `기간 질문 ${num(hidden)}개가 더 있어요 — 날짜를 누르면 그날 질문을 봅니다.` }));
        grid.append(box);
      }
    }
    $('calendar').replaceChildren(grid);
    state.flashId = null;  // 강조는 등록 직후 한 번
  }
  /** 막대를 누르면 그 질문 패널 — 운영자 이상은 편집 폼(문장·우선 노출·사용·기간), 뷰어는 읽기. 저장하면 캘린더를 다시 읽는다. */
  const SCHEDULE_DETAIL = [{ key: 'label', label: '분야' }, { key: 'text', label: '문장' }, { key: 'pinned', label: '고정', format: (v) => PINS[v]?.label ?? '-' },
    { key: 'valid_from', label: '노출 시작일', format: (v) => v ?? '처음부터' }, { key: 'valid_until', label: '노출 종료일', format: (v) => v ?? '계속' }];
  async function openSchedule(item) {
    const editor = hasRole('editor');
    if (!(await openDialog('초기 질문', { ...item }, editor ? SCHEDULE_DETAIL.slice(0, 1) : SCHEDULE_DETAIL))) return;
    manage.starterPanel({ ...item });
  }
  /** 날짜 칸(격자·목록 공용) — 누르면 패널, 기간 선택 중이면 종료일. */
  function dayButton(cell, cls) {
    const box = el('div', `${cls} ${cell.kind}${inRange(cell.date) ? ' in-range' : ''}${state.range?.start === cell.date ? ' range-start' : ''}`);
    box.tabIndex = 0;
    box.setAttribute('role', 'button');
    box.dataset.date = cell.date;
    const head = el('span', 'cal-date', cell.date.slice(8).replace(/^0/, ''));
    if (cell.kind === 'today') head.append(el('em', null, '오늘'));
    const summary = daySummary(cell);
    box.append(head, summary);
    box.setAttribute('aria-label', `${cell.date} ${[...summary.childNodes].map((node) => node.textContent).join(' · ')}${summary.title ? ` · ${summary.title}` : ''}`);
    box.onclick = () => dayClicked(cell.date);
    box.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); dayClicked(cell.date); } };
    return box;
  }
  /** 좁은 화면 — 주 단위 목록(아젠다). 막대는 그 주에서 시작하는 날에 '라벨 · 문장 ~종료일' 글자로. */
  function agenda(weeks, cells) {
    const list = el('div', 'cal-agenda');
    for (const weekStart of weeks) {
      const days = [];
      for (let i = 0; i < 7; i++) if (cells.has(addDays(weekStart, i))) days.push(cells.get(addDays(weekStart, i)));
      const placed = lanesFor(days[0].date, days.at(-1).date);
      list.append(el('h4', 'cal-week', `${days[0].date.slice(5)} – ${days.at(-1).date.slice(5)}`));
      for (const cell of days) {
        const row = dayButton(cell, 'cal-day-row');
        const head = row.querySelector('.cal-date');
        head.textContent = `${cell.date.slice(5)} (${WEEKDAYS[weekdayOf(cell.date)]})`;
        if (cell.kind === 'today') head.append(el('em', null, '오늘'));
        for (const bar of placed.filter((b) => b.start === cell.date)) {
          const line = el('span', `cal-bar-line${PINS[bar.item.pinned]?.rank ? ` pinned pin-${bar.item.pinned}` : ''}`);
          line.append(barLabel(bar.item, true));
          row.append(line);
        }
        list.append(row);
      }
    }
    return list;
  }

  // ── 기간 선택(운영자 이상): 날짜 패널의 [이 날부터 기간 선택] → 다음 칸 클릭이 종료일(그 칸 패널은 안 연다)
  // → [이 기간에 질문 추가]가 수동 추가 폼을 기간 채워 연다. Esc·[선택 해제]로 취소.
  function dayClicked(iso) {
    if (state.range && !state.range.end) {
      if (iso < state.calendar.today) { $('cal-range-note').textContent = `시작일 ${state.range.start} — 오늘 이후의 종료일을 누르세요`; return; }
      state.range = iso < state.range.start ? { start: iso, end: state.range.start } : { start: state.range.start, end: iso };
      renderCalendar();
      return;
    }
    openDayPanel(iso);
  }
  const daysBetween = (a, b) => Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 864e5) + 1;
  function syncRange(hover) {
    const range = state.range;
    $('cal-range').hidden = !range;
    // 선택 중엔 끄기만 한다(숨기면 좁은 화면에서 도구 줄이 접혀 달력이 위로 밀린다).
    $('cal-add').hidden = !hasRole('editor');
    $('cal-add').disabled = !!range;
    if (!range) return;
    const [from, to] = range.end ? [range.start, range.end] : hover ? [range.start, hover].sort() : [range.start, range.start];
    $('cal-range-note').textContent = range.end ? `기간 ${from} ~ ${to} · ${num(daysBetween(from, to))}일`
      : `시작일 ${range.start} — 종료일을 누르세요${hover ? ` · ${num(daysBetween(from, to))}일` : ''}`;
    $('cal-range-add').hidden = !range.end;
  }
  /** 종료일 고르는 중 — 가리킨 칸까지 구간을 미리 칠한다(다시 그리지 않고 클래스만). */
  $('calendar').addEventListener('mouseover', (event) => {
    if (!state.range || state.range.end) return;
    const hover = event.target.closest('[data-date]')?.dataset.date;
    if (!hover || hover < state.calendar.today) return;
    const [from, to] = [state.range.start, hover].sort();
    for (const node of $('calendar').querySelectorAll('[data-date]')) node.classList.toggle('in-range', node.dataset.date >= from && node.dataset.date <= to);
    syncRange(hover);
  });
  function clearRange() { state.range = null; if (state.calendar) renderCalendar(); }
  $('cal-range-clear').onclick = clearRange;
  $('cal-range-add').onclick = () => {
    const { start, end } = state.range;
    clearRange();
    manage.openStarterCreate({ valid_from: start, valid_until: end }, { estimate: estimateProbability });
  };
  for (const [id, step] of [['cal-prev', -1], ['cal-next', 1]]) $(id).onclick = () => {
    const [y, m] = state.calMonth.split('-').map(Number), moved = new Date(Date.UTC(y, m - 1 + step, 1));
    state.calMonth = moved.toISOString().slice(0, 7);
    loadCalendar();
  };
  $('cal-today').onclick = () => { state.calMonth = null; loadCalendar(); };
  // 주 동작 — 기간 질문 한 번에 추가(시작일 오늘, 종료·문장·분야·고정을 같은 폼에서, 예상 확률 표시).
  $('cal-add').onclick = () => manage.openStarterCreate({ valid_from: state.calendar?.today ?? localDay() }, { estimate: estimateProbability });

  /** 등록 전 예상 확률 — 그 날(시작일, 없으면 오늘) 풀에 가상 행을 넣어 서버가 같은 추첨으로 센다. */
  async function estimateProbability({ slot, pinned, date }) {
    const today = state.calendar?.today ?? localDay();
    const day = date && date > today ? date : today;
    const data = await api(`/admin/api/starters/preview?${new URLSearchParams({ date: day, seed: 1, add_slot: slot, add_pinned: pinned })}`);
    return { date: data.date, probability: data.expected_probability };
  }

  // ── 날짜 패널: 그날 미리보기 + 슬롯별 풀과 확률. 운영자 이상은 행을 고정/해제·중지(기존 PATCH·DELETE)하고
  // 오늘 이후 날짜면 여기서 기간 선택을 시작한다.
  const DAY_KINDS = { today: '오늘', future: '미래 · 예상치', past: '과거 · 생성 기록' };
  async function openDayPanel(iso) {
    const cell = state.calendar.days.find((day) => day.date === iso);
    if (!(await openDialog(`${iso} · ${DAY_KINDS[cell.kind]}`))) return;
    // 개요(분야 목록) → 예시(첫 화면 한 벌) → 동작(기간 선택) 순. 분야 목록은 미리보기 응답으로 채운다.
    // 저장 뒤 다시 읽은 패널은 같은 스크롤 자리로 돌아간다(state.dayRestore — setPin이 남긴다, 한 번 쓰고 지운다).
    const box = $('record-actions'), pool = el('div', 'day-pool'), fill = (data) => {
      pool.replaceChildren(...dayPool(data, () => openDayPanel(iso)));
      if (state.dayRestore) { $('record-dialog').scrollTop = state.dayRestore.scroll; state.dayRestore = null; }
    };
    // 오늘의 첫 화면 예시는 달력 위 '지금 첫 화면'이 늘 보인다 — 패널엔 분야 목록만(같은 예시를 두 번 두지 않는다).
    if (cell.kind === 'today') {
      pool.id = 'day-pool';  // request()가 오류 문구·다시 시도를 여기에 그린다
      box.append(pool);
      request('day-pool', `/admin/api/starters/preview?${new URLSearchParams({ date: iso, seed: 1 })}`, fill);
    } else box.append(pool, previewView(iso, 'day-preview', fill));
    if (hasRole('editor') && cell.kind !== 'past') {
      const tools = el('div', 'day-tools'), pick = el('button', 'crema-button', '이 날부터 기간 선택');
      pick.type = 'button';
      pick.onclick = () => { state.range = { start: iso }; closePanel(); renderCalendar(); };
      tools.append(pick, el('span', 'analysis-note', '다음에 누르는 날짜가 종료일입니다.'));
      box.append(tools);
    }
  }
  /** 확률 칸 — 막대(progress) + 글자. CSP(인라인 style 금지) 아래라 폭 대신 progress 값으로 그린다. */
  function probabilityCell(cls, probability, text) {
    const cell = el('span', `${cls}${probability === 0 ? ' zero' : ''}`, text);
    if (probability > 0) {
      const meter = el('progress', 'day-meter');
      meter.max = 1;
      meter.value = probability;
      meter.setAttribute('aria-hidden', 'true');
      cell.prepend(meter);
    }
    return cell;
  }
  // ── 날짜 패널의 분야 목록: 개요(뽑히는 규칙 한 줄 + 분야 · 질문 수 · 고정, 이름순) → 한 분야를 펼쳐 → 질문을
  // 고른다. 펼친 분야·검색어·필터는 state에 남아 패널을 다시 열어도(고정 후 다시 읽기 포함) 그대로다.
  const SLOT_TOOLS_MIN = 10;  // 분야가 이보다 많을 때만 검색·필터를 보인다(화면 배치 상수)
  function dayPool(data, reopen) {
    if (!data.rows.length) return [placeholder(data.mode === 'record' ? '이 날 생성된 자동 질문이 없습니다.' : '이 날 노출할 초기 질문이 없습니다.')];
    const record = data.mode === 'record', editable = hasRole('editor') && !record;
    const bySlot = new Map();
    for (const row of data.rows) (bySlot.get(row.slot) ?? bySlot.set(row.slot, []).get(row.slot)).push(row);
    // 분야는 이름순(분야 확률은 균등 추첨이라 거의 같아 순서 정보가 없다 — 머리말에 한 번만 쓴다),
    // 분야 안 질문은 확률 내림차순(같으면 고정 먼저 · 문장). 확률이 날짜 시드라 열 때마다 같은 순서다.
    for (const rows of bySlot.values()) rows.sort((a, b) => (b.probability ?? 0) - (a.probability ?? 0) || pinRank(b) - pinRank(a) || a.text.localeCompare(b.text, 'ko'));
    const slots = [...data.slots].sort((a, b) => a.label.localeCompare(b.label, 'ko'));
    // 분야 확률 = 하루 질문 수 ÷ 분야 수(분야는 균등하게 섞인다 — 중복 건너뜀 같은 드문 경우만 조금 다르다).
    const slotChance = slotProbability(data);
    const head = el('div', 'slot-head'), count = el('h4', 'day-heading'), list = el('div', 'slot-list');
    head.append(count);
    // 뽑히는 규칙 한 줄 — 첫 화면 고정이 먼저 칸을 차지하고, 남은 칸을 남은 분야에서 무작위로(분야 고정이 있으면 그 질문).
    const used = data.global_pins?.used ?? 0, freeSlots = slots.length - (data.global_pins?.slots ?? 0);
    // '분야마다 약 x%'는 첫 화면 고정이 있을 때만 강조해 보인다(그때 값이 n/분야 수에서 달라진다). 분야 줄마다는 싣지 않는다(모두 같다).
    const rule = record ? null : el('p', 'slot-rule', `${used ? `첫 화면 고정 ${num(used)}개가 날마다 먼저 나오고, 나머지 ` : '매일 '}분야 ${num(freeSlots)}개 중 ${num(Math.min(Math.max(0, data.n - used), freeSlots))}개가 무작위로 뽑혀요`);
    if (rule && used) rule.append(' — ', el('b', null, `분야마다 약 ${chanceText(slotChance)}`));
    rule?.append('. 뽑힌 분야에서 질문 하나가 나오고, 분야 대표 고정이 있으면 그 질문이 나와요.');
    const tools = el('div', 'slot-tools'), search = el('input'), pinnedOnly = el('input'), pinnedLabel = el('label', 'check');
    search.type = 'search';
    search.placeholder = '분야 이름 검색';
    search.setAttribute('aria-label', '분야 이름 검색');
    search.value = state.slotQuery;
    pinnedOnly.type = 'checkbox';
    pinnedOnly.checked = state.pinnedSlotsOnly;
    pinnedLabel.append(pinnedOnly, ' 고정 있는 분야만');
    tools.append(search, pinnedLabel);
    const render = () => {
      const query = state.slotQuery.trim().toLowerCase();
      const shown = slots.filter((slot) => (!query || slot.label.toLowerCase().includes(query)) && (!state.pinnedSlotsOnly || slot.pinned));
      count.textContent = shown.length === slots.length ? poolCount(data) : `분야 ${num(shown.length)} / ${num(slots.length)}개`;
      list.replaceChildren(...(shown.length ? shown.map(slotItem) : [placeholder('조건에 맞는 분야가 없습니다.')]));
    };
    search.oninput = () => { state.slotQuery = search.value; render(); };
    pinnedOnly.onchange = () => { state.pinnedSlotsOnly = pinnedOnly.checked; render(); };
    render();
    const sticky = el('div', 'slot-sticky');
    sticky.append(...(slots.length >= SLOT_TOOLS_MIN ? [tools] : []), head);
    return [...(rule ? [rule] : []), sticky, list];

    /** 분야 한 줄(접힘) — ▸ 이름 · 질문 수 · (고정 질문). 누르면 그 분야 질문이 아래로 펼쳐진다. */
    function slotItem(slot) {
      const rows = bySlot.get(slot.slot) ?? [], item = el('div', 'slot-item'), toggle = el('button', 'slot-row'), body = el('div', 'slot-body');
      toggle.type = 'button';
      const name = el('span', 'slot-name');
      name.append(el('span', 'slot-caret'), el('b', null, slot.label), el('small', null, `질문 ${num(rows.length)}`));
      toggle.append(name);
      const pinned = rows.filter(pinRank).sort((a, b) => pinRank(b) - pinRank(a));
      if (pinned.length) {
        const mark = el('span', 'slot-pin');
        mark.append(pinBadge(pinned[0].pinned), el('span', null, pinned[0].text));
        if (pinned.length > 1) mark.append(` 외 ${num(pinned.length - 1)}`);
        mark.title = pinned.map((row) => `${PINS[row.pinned].label} · ${row.text}`).join(' / ');
        toggle.append(mark);
      }
      const sync = () => {
        const open = state.openSlots.has(slot.slot);
        item.classList.toggle('open', open);
        toggle.setAttribute('aria-expanded', String(open));
        body.hidden = !open;
        // 질문 줄은 처음 펼칠 때 만든다(분야가 수십 개라 미리 다 그리지 않는다).
        if (open && !body.childElementCount) {
          body.append(...rows.map(rowLine));
          // 하던 고정 확인(상한 목록에서 다른 고정을 푼 뒤 다시 읽은 경우 등)은 같은 행에 다시 띄운다.
          const pending = state.pendingPin && rows.find((row) => row.id === state.pendingPin.id);
          if (pending) confirmPin(pending, body.querySelector(`.day-row[data-id="${pending.id}"]`), state.pendingPin.kind);
        }
      };
      toggle.onclick = () => { state.openSlots[state.openSlots.has(slot.slot) ? 'delete' : 'add'](slot.slot); sync(); };
      sync();
      item.append(toggle, body);
      return item;
    }
    /** 질문 한 줄 — 문장 · 확률(0이면 이유, 기록은 등록 방식) · (운영자) 관리 ▾. */
    function rowLine(row) {
      const line = el('div', `day-row${pinRank(row) ? ' pinned' : ''}${state.dayRestore?.flash === row.id ? ' flash' : ''}`), text = el('span', 'day-text'), side = el('span', 'day-side');
      line.dataset.id = row.id;
      if (pinRank(row)) text.append(pinBadge(row.pinned), ' ');
      text.append(row.text);
      side.append(probabilityCell('day-prob', row.probability, row.probability == null ? SOURCE_KINDS[row.source] ?? row.source : probabilityText(row)));
      line.append(text, side);
      // 주 동작(고정/고정 해제)은 바로 보이는 버튼, 노출 끄기는 작은 보조 링크(메뉴 단계 없이).
      if (editable) {
        // 고정은 작은 선택 하나(고정 안 함 · 분야 고정 · 첫 화면 고정) — 거는 쪽은 그 자리에서 영향을 보이고 확인받는다.
        const pin = el('select', 'pin-select'), stop = el('button', 'link-button subtle', '노출 끄기');
        pin.setAttribute('aria-label', '고정');
        pin.append(...Object.entries(PINS).map(([kind, { label }]) => new Option(label, kind, false, kind === row.pinned)));
        stop.type = 'button';
        pin.onchange = () => { const kind = pin.value; pin.value = row.pinned; kind ? confirmPin(row, line, kind) : setPin(row, kind, reopen); };
        stop.onclick = () => stopStarter(row, reopen);
        const actions = el('span', 'day-actions');
        actions.append(pin, stop);
        side.append(actions);
      }
      return line;
    }
    /** 고정 확인 — 그 줄 바로 아래에서 무엇이 바뀌는지(목록 편집 패널과 같은 문구) 보이고 확인받는다. */
    function confirmPin(row, line, kind) {
      if (line.nextElementSibling?.classList.contains('pin-confirm')) line.nextElementSibling.remove();
      // 확인 중인 선택은 상태에 남긴다 — 다시 읽어도(다른 고정 해제 뒤) 같은 선택·확인 박스로 돌아온다.
      state.pendingPin = { id: row.id, kind };
      const select = line.querySelector('.pin-select');
      if (select) select.value = kind;
      const box = el('div', 'pin-confirm'), ok = el('button', 'crema-button primary', PINS[kind].label), cancel = el('button', 'crema-button', '취소');
      box.setAttribute('role', 'group');
      box.setAttribute('aria-label', '고정 확인');
      ok.type = cancel.type = 'button';
      const error = el('p', 'error');  // 저장 오류는 이 자리에(맨 위 띠가 아니라)
      error.hidden = true;
      ok.onclick = () => setPin(row, kind, reopen, (message) => { error.textContent = message; error.hidden = false; });
      cancel.onclick = () => { state.pendingPin = null; if (select) select.value = row.pinned; box.remove(); };
      box.append(el('p', null, manage.pinNotice(data, row.slot, row.id, kind)), error);
      // 상한·분야당 하나에 걸리면 확인은 끄고, 지금 첫 화면 고정을 그 자리에서 풀 수 있게 보인다.
      const blockers = manage.pinBlockers(data, row.slot, row.id, kind);
      if (blockers) {
        ok.disabled = true;
        const list = el('ul', 'pin-blockers');
        list.append(...blockers.map((item) => {
          const li = el('li', null, `${item.label} · ${item.text} `), release = el('button', 'link-button', '해제');
          release.type = 'button';
          release.onclick = () => setPin({ id: item.id, pinned: 'global' }, '', reopen, (message) => { error.textContent = message; error.hidden = false; });
          li.append(release);
          return li;
        }));
        box.append(list);
      }
      box.append(ok, cancel);
      line.after(box);
      (blockers ? cancel : ok).focus();
    }
  }
  /** 패널의 쓰기 — 기존 편집 API(expected 동봉). 성공하면 캘린더와 패널을 다시 읽고, 실패는 서버 문구. */
  async function starterWrite(path, method, body, reopen, report = showError) {
    try {
      await api(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      state.preview = null;  // 오늘 확률·고정 경고가 바뀐 풀로 다시 계산되게
      await loadCalendar();
      reopen();
      return true;
    } catch (error) { if (error.name !== 'AbortError') report(errorText(error, '저장하지 못했습니다. 다시 시도해 주세요.')); return false; }
  }
  /** 고정 바꾸기(이름 — '' · slot · global) — 끝나면 TOAST_MS 동안 '되돌리기'(같은 API로 이전 값, expected 동봉). */
  async function setPin(row, kind, reopen, report) {
    if (state.pendingPin?.id === row.id) state.pendingPin = null;  // 확인한 그 행을 저장하면 하던 확인은 끝
    // 다시 읽은 패널이 같은 자리(스크롤·펼친 분야)로 돌아오고 바뀐 행을 잠깐 강조한다(openDayPanel이 쓴다).
    const write = (from, to) => { state.dayRestore = { scroll: $('record-dialog').scrollTop, flash: row.id }; return starterWrite(`/admin/starters/${row.id}`, 'PATCH', { changes: { pinned: to }, expected: { pinned: from } }, reopen, report); };
    if (await write(row.pinned, kind)) showToast(kind ? `${PINS[kind].label}했습니다.` : '고정을 풀었습니다.', { label: '되돌리기', run: () => write(kind, row.pinned) });
  }
  /** 노출 끄기 — 영향(첫 화면 후보에서 빠짐·다시 켜는 곳)을 먼저 보이고 확인받는다. */
  async function stopStarter(row, reopen) {
    const ok = await confirmDialog({
      title: '이 질문의 노출을 끌까요?', confirmLabel: '노출 끄기', danger: true,
      message: `"${row.text}"\n첫 화면에 바로 나오지 않게 됩니다. 다시 켜려면 목록에서 이 질문을 열어 '노출 켜기'를 체크하세요.`,
    });
    if (ok && await starterWrite(`/admin/starters/${row.id}`, 'DELETE', { expected: { active: true } }, reopen)) showToast('노출을 껐습니다.');
  }

  // ── 감사 로그(관리자 전용): 서버가 준 행(target_type·action·before/after JSON)을 사람이 읽는 줄로 옮긴다.
  // 라벨 표는 이 한 곳이다 — 모르는 값은 원문 그대로 보인다(새 작업이 생겨도 빈칸이 되지 않게).
  const CLI_LABEL = '서버 관리 도구(CLI)';
  const AUDIT_TARGETS = { '': '전체', login: '로그인', admin: '계정', user: '회원', starter: '초기 질문', session: '대화' };
  // 작업명은 동사만 — 무엇에 대한 작업인지는 바로 옆 '대상' 칸("초기 질문 #151 · …", "계정 owner")이 말한다.
  const AUDIT_ACTIONS = {
    'login:ok': '로그인', 'login:failed': '로그인 실패', 'login:logout': '로그아웃',
    'admin:create': '생성', 'admin:update': '변경', 'admin:password_reset': '비밀번호 재설정', 'admin:password_change': '비밀번호 변경',
    'user:update': '변경',
    'starter:create': '직접 등록', 'starter:update': '수정', 'starter:deactivate': '노출 끄기', 'starter:generate': '자동 생성',
    'session:purge': '영구 삭제',
  };
  const AUDIT_FIELDS = {
    username: '계정명', role: '역할', is_active: '활성', note: '메모', text: '문장', pinned: '고정', active: '노출 설정', valid_from: '노출 시작일', valid_until: '노출 종료일',
    slot: '분야', status: '결과', inserted: '추가 수', force: '다시 생성', user_id: '회원번호', turns: '질의 수', deleted_at: '삭제 요청 시각', app_name: '앱',
  };
  const auditValue = (field, value) => (value == null ? '-' : field === 'role' ? roleLabel(value) : field === 'slot' ? slotLabel(value) : field === 'status' ? GENERATE_STATUS[value] ?? value : field === 'active' ? (value ? '켜짐' : '꺼짐') : field === 'pinned' ? PINS[value === true ? 'slot' : value === false ? '' : value]?.label ?? String(value) : typeof value === 'boolean' ? (value ? '예' : '아니오') : typeof value === 'object' ? JSON.stringify(value) : String(value));
  /** 바뀐 필드 — [라벨, 이전, 이후]. 'by'(CLI 표시)는 계정 칸이 말하므로 뺀다. */
  const auditChanges = (row) => [...new Set([...Object.keys(row.before ?? {}), ...Object.keys(row.after ?? {})])].filter((field) => field !== 'by')
    .map((field) => [AUDIT_FIELDS[field] ?? field, row.before && field in row.before ? auditValue(field, row.before[field]) : null, row.after && field in row.after ? auditValue(field, row.after[field]) : null]);
  /** 대상 — 종류 앞말 + 사람이 읽는 이름. 로그인 기록은 실제 계정일 때만 이름이 온다(서버가 입력 원문을
   *  싣지 않는다 — 아이디 칸에 친 비밀번호가 보이지 않게). 세션은 id 앞 8자, 초기 질문은 #id. */
  function auditTarget(row) {
    const name = row.target_name;
    if (name == null) return row.target_type === 'login' ? '알 수 없는 계정' : `${AUDIT_TARGETS[row.target_type] ?? row.target_type} -`;
    const noun = row.target_type === 'login' ? AUDIT_TARGETS.admin : AUDIT_TARGETS[row.target_type] ?? row.target_type;
    // 초기 질문 대상은 행 id(#n) 또는 분야 키(지금 생성 — 분야 이름으로).
    // 초기 질문 행이면 문장 앞부분을 함께(지금 행의 문장 — 지워졌으면 #id만).
    if (row.target_type === 'starter') return `${noun} ${/^\d+$/.test(name) ? `#${name}${row.target_text ? ` · ${row.target_text}` : ''}` : slotLabel(name)}`;
    return `${noun} ${row.target_type === 'session' ? String(name).slice(0, 8) : name}`;
  }
  const auditRow = (row) => ({
    ...row,
    who: row.actor_name ?? (row.after?.by === 'cli' ? CLI_LABEL : row.target_type === 'login' ? '-' : '시스템'),
    what: AUDIT_ACTIONS[`${row.target_type}:${row.action}`] ?? `${row.target_type} ${row.action}`,
    target: auditTarget(row),
    change: auditChanges(row).map(([label, before, after]) => `${label}: ${[before, after].filter((v) => v != null).join(' → ')}`).join(' · ') || '-',
  });
  const AUDIT_COLUMNS = [
    { key: 'created_at', label: '시각', format: when }, { key: 'who', label: '계정', text: (row) => (row.who === CLI_LABEL ? Object.assign(el('span', null, 'CLI'), { title: CLI_LABEL }) : row.who) },  // 표 칸은 짧게, 긴 이름은 툴팁·필터·CSV·상세 { key: 'what', label: '작업' },
    { key: 'target', label: '대상' }, { key: 'change', label: '변경 내용' }, { key: 'ip', label: 'IP', format: day },
  ];
  async function openAudit(row) {
    const record = { 시각: stamp(row.created_at), 계정: row.who, 작업: row.what, 대상: row.target, IP: row.ip ?? '-' };
    for (const [label, before, after] of auditChanges(row)) record[label] = `${before ?? '-'} → ${after ?? '-'}`;
    // 패널 제목은 대상 종류 + 작업("초기 질문 노출 끄기") — 표의 작업 칸은 동사만이라 여기서 붙인다.
    const title = row.target_type === 'login' ? row.what : `${AUDIT_TARGETS[row.target_type] ?? row.target_type} ${row.what}`;
    if (!(await openDialog(`감사 기록 · ${title}`, record))) return;
    // 초기 질문 행 기록이면 그 질문으로 — 목록(노출 상태 전체)을 그 문장으로 걸러 연다.
    if (row.target_type === 'starter' && row.target_text) {
      const go = el('button', 'crema-button', '이 질문 보기');
      go.type = 'button';
      go.onclick = () => { pushHistory(); state.pendingStarter = String(row.target_name); setView('starters', 'list'); openList('starters', { id: row.target_name, status: 'all' }); };
      $('record-actions').append(go);
    }
  }
  const downloadAudit = () => downloadList($('audit-csv'), 'audit', '/admin/api/audit',
    [`시각 (${state.tz.label})`, ...AUDIT_COLUMNS.slice(1).map((column) => column.label)],
    // 화면의 빈 칸 표시('-')는 파일에선 빈 칸 — 선두 '-'는 셀 규칙이 '-로 감싼다.
    (raw) => { const row = auditRow(raw); return [stamp(row.created_at), ...AUDIT_COLUMNS.slice(1).map((column) => (row[column.key] == null || row[column.key] === '-' ? '' : row[column.key]))]; });
  /** 계정 선택지 = 기록에 남은 계정 이름(서버 actors, 스냅샷 이름). 처음 한 번 채운다. */
  // 로그인·로그아웃 행은 열어도 더 볼 것이 없다(바뀐 필드 없음) — 누를 수 없는 행으로 둔다.
  openAudit.enabled = (row) => row.target_type !== 'login';
  function loadAudit() {
    return loadList('audit', `/admin/api/audit?${new URLSearchParams(state.applied.audit)}`, AUDIT_COLUMNS, openAudit, auditRow, (data) => {
      addMissingOptions($('audit-actor'), [...data.actors, ...(data.cli_actor ? [data.cli_actor] : [])], (value) => (value === data.cli_actor ? CLI_LABEL : value));
      state.starterSlots = data.slots ?? state.starterSlots;
    });
  }

  async function openUser(row) {
    if (!(await openDialog(`회원 ${row.nickname ?? row.user_no}`, row, USER_DETAIL))) return;
    // 대화 탭의 검색이 회원번호 일치를 받는다 — 그 회원의 대화만 걸고 첫 대화를 연다.
    const sessions = el('button', null, '이 회원의 대화 보기');
    sessions.onclick = () => openSessions({ q: row.user_no });
    $('record-actions').append(sessions);
  }

  async function openStarter(row) {
    const columns = starterDetail();
    if (!(await openDialog('초기 질문', row, hasRole('editor') ? columns.filter((column) => STARTER_READONLY_KEYS.includes(column.key)) : columns))) return;
    manage.starterPanel(row);
    // 맨 위 상태 요약 한 덩어리(분야 · 지금 노출 여부와 이유 · 첫 화면 확률 · 고정) → 편집 폼 → 읽기 표.
    const summary = el('div', `status-banner${row.live ? ' live' : ''}`), status = el('p');
    status.append(el('b', null, row.live ? '지금 노출 중' : '노출 안 됨'), row.live ? ' · 첫 화면 확률 ' : ` — ${notLiveReason(row.not_live_reason)}`);
    if (row.live) status.append(listProbability(row), infoMark(PROBABILITY_HINT));
    summary.append(status, el('p', 'status-meta', `${row.label} · ${PINS[row.pinned]?.label ?? '고정 안 함'}`));
    $('record-actions').prepend(summary);
  }


  function readQuery(name) {
    const values = {};
    for (const [key, id] of Object.entries(queryForms[name].fields)) {
      if ($(id).closest('label')?.hidden || $(id).disabled) continue;
      if ($(id).value.trim()) values[key] = $(id).value.trim();
    }
    return values;
  }

  const queryLabels = {q: '검색', since: '기간', rating: '피드백', status: '상태', source: '등록 방식', term: '노출 기간', slot: '분야', id: '질문 번호', target: '작업', actor: '계정', rbti: 'RBTI', nickname: '닉네임'};
  const PRESENCE = { any: '있음', none: '없음' };
  /** 칩 값 글자 — 값 세그먼트(숨은 칸)는 그 버튼 글자가 정본이다(라벨 표를 따로 두지 않는다). */
  function queryValue(name, key, value) {
    const input = $(queryForms[name].fields[key]);
    if (input.dataset.segment) return [...$(input.dataset.segment).children].find((button) => button.dataset.value === value)?.textContent ?? value;
    // 선택 상자는 그 선택지 글자(슬롯 키 → 칩 라벨, 노출 상태 → '지금 노출 중').
    return PRESENCE[value] ?? (input.tagName === 'SELECT' ? [...input.options].find((option) => option.value === value)?.textContent : null) ?? value;
  }

  /** 적용 중인 조건을 칩(키: 값 ×)으로 — 칩은 그 조건만, '초기화'는 전부 풀고 다시 읽는다. 기본 조건(전체)이면 줄째 숨긴다. */
  function queryNote(name) {
    const applied = state.applied[name] || {}, field = (key) => $(queryForms[name].fields[key]);
    // 검색어는 입력하는 대로 반영되므로(searchTyping) '조건 변경됨'에서 뺀다.
    const { q: typed, ...pending } = readQuery(name), { q: kept, ...current } = applied;
    const dirty = JSON.stringify(pending) !== JSON.stringify(current);
    const note = $('query-note-' + name);
    // 기간(시작·종료)은 칩 하나 '기간: A ~ B' — 해제도 둘 함께(clearCondition).
    const { since, until, ...rest } = applied;
    // '전체' 값(data-clear — 기본값이 있는 칸을 푼 상태)은 칩이 아니다.
    const entries = [...(since || until ? [['since', `${since ?? ''} ~ ${until ?? ''}`.trim()]] : []), ...Object.entries(rest).filter(([key, value]) => value !== field(key).dataset.clear)];
    const chips = entries.map(([key, value]) => {
      const chip = el('button', 'chip', `${queryLabels[key] || key}: ${key === 'since' ? value : queryValue(name, key, value)}`);
      chip.type = 'button';
      chip.setAttribute('aria-label', `${queryLabels[key] || key} 조건 해제`);
      chip.onclick = () => clearCondition(name, key);
      return chip;
    });
    const reset = el('button', 'link-button', '초기화');
    reset.type = 'button';
    reset.onclick = () => resetQuery(name);
    // '초기화'는 기본값과 다른 조건이 있을 때만(기본 조건 칩 '지금 노출 중'만 있으면 누를 일이 없다).
    const changed = entries.some(([key, value]) => key === 'since' || value !== field(key).dataset.default);
    note.replaceChildren(...(dirty ? [el('span', 'dirty', '조건 변경됨 · 조회를 눌러 적용하세요.')] : []), ...chips, ...(changed ? [reset] : []));
    note.hidden = !dirty && !chips.length;
  }
  /** 조건 칸 하나 비우기 — 세그먼트로 고르는 값(data-segment)은 그 묶음의 '전체'로, 직접 기간 칸은 닫는다. */
  /** 칸 비우기 — 기본값이 있는 칸(노출 상태 = 지금 노출 중)은 '전체' 값(data-clear)으로, reset이면 기본값으로. */
  function clearField(input, reset = false) {
    input.value = (reset ? input.dataset.default : input.dataset.clear) ?? '';
    if (input.dataset.segment) press($(input.dataset.segment), $(input.dataset.segment).firstElementChild);
    input.closest('.crema-custom')?.setAttribute('hidden', '');
  }
  /** 조건 하나 해제 — 기간은 시작·종료를 함께 푼다. */
  function clearCondition(name, key) {
    const { form, fields } = queryForms[name];
    for (const field of key === 'since' || key === 'until' ? ['since', 'until'] : [key]) clearField($(fields[field]));
    $(form).requestSubmit();
  }
  /** 조건 전부 해제(칩 줄·빈 목록의 '초기화'). submit=false면 칸만 비운다(다른 화면이 조건을 새로 걸 때). */
  function resetQuery(name, submit = true) {
    const { form, fields } = queryForms[name];
    for (const id of Object.values(fields)) clearField($(id), true);
    if (submit) $(form).requestSubmit();
  }
  /** 칸 값 → 세그먼트 눌림 표시 — 기간 칸은 값이 있으면 '직접'(마지막 버튼)을 누르고 칸을 연다. */
  function syncSegments(name) {
    for (const input of Object.values(queryForms[name].fields).map($).filter((node) => node.dataset.segment)) {
      const group = $(input.dataset.segment), custom = input.closest('.crema-custom');
      if (custom) {
        const open = [...custom.querySelectorAll('input')].some((node) => node.value);
        custom.hidden = !open;
        press(group, open ? group.lastElementChild : group.firstElementChild);
      } else press(group, [...group.children].find((button) => button.dataset.value === input.value) ?? group.firstElementChild);
    }
  }
  /** RBTI 배지를 누르면 그 유형으로 거르고, 같은 유형이 걸려 있으면 푼다(목록 카드·회원 표 공용). */
  function toggleRbti(name, code) {
    const { form, fields } = queryForms[name];
    $(fields.rbti).value = state.applied[name]?.rbti === code ? '' : code;
    $(form).requestSubmit();
  }
  /** 데이터에서 오는 선택지(RBTI 코드·감사 계정) — 아직 없는 값만 덧붙인다(해시가 먼저 넣은 값과 겹치지 않게).
   *  label이 있으면 글자는 그것으로(이미 있는 선택지도 글자만 맞춘다 — RBTI 코드 · 유형 이름). */
  function addMissingOptions(select, values, label = (value) => value) {
    const known = new Map([...select.options].map((option) => [option.value, option]));
    for (const value of values) known.has(value) ? (known.get(value).textContent = label(value)) : select.append(new Option(label(value), value));
  }
  /** 지금 생성 결과 상태 → 글자(감사 '결과'·생성 결과 표 공용). */
  const GENERATE_STATUS = { ok: '완료', failed: '실패', skipped: '건너뜀' };
  /** 분야 키 → 이름(서버가 목록·캘린더·감사 응답에 싣는 slots — chip_label 규칙). 모르면 키 그대로. */
  const slotLabel = (key) => state.starterSlots.find((slot) => slot.slot === key)?.label ?? key;
  /** RBTI 코드 · 유형 이름(서버 /me rbti_names — rbti.persona가 정본). 모르는 코드는 코드만. */
  const rbtiLabel = (code) => (state.rbtiNames[code] ? `${code} · ${state.rbtiNames[code]}` : code);

  function applyQuery(name) {
    state.applied[name] = readQuery(name);
    state[queryForms[name].page] = 0;
    queryNote(name);
  }

  for (const [name, config] of Object.entries(queryForms)) {
    const note = el('p', 'query-note');
    note.id = 'query-note-' + name;
    note.setAttribute('role', 'status');
    $(config.form).after(note);
    $(config.form).addEventListener('input', () => queryNote(name));
    $(config.form).addEventListener('change', () => queryNote(name));
    applyQuery(name);
  }

  // ── 통계: 어드민 시간대 일별 보고(고객사 엑셀 양식 11열). 집계·빈 날 채움·기간 요약은 서버(/admin/api/stats)가 한다.
  // 화면 구성은 crema-ai-admin Statistics.tsx를 따른다(카드 → 추이 → 요일 막대·인사이트 → 표). 색은 --crema-* 토큰 클래스.
  const fixed2 = (value) => value == null ? '-' : value.toFixed(2);
  const average = (value) => value == null ? '-' : Number(value.toFixed(2)).toLocaleString('ko-KR');
  // 건수는 화면에선 천 단위 구분, CSV에선 원값(엑셀이 수로 읽게). unit은 카드의 단위 글자, tone은 차트 색 클래스.
  // excel은 엑셀 머리(고객 양식 그대로 — 화면 용어 통일에서 제외).
  const counted = (key, label, excel, unit, tone) => ({ key, label, excel, unit, tone, format: num, csv: String });
  const STAT_COLUMNS = [
    { key: 'day', label: '날짜', excel: '날짜', format: String, csv: (day) => day.replaceAll('-', '.') }, { key: 'weekday', label: '요일', excel: '요일', format: String },
    counted('sessions', `${TERMS.sessions} 수`, '세션수', '개', 'tone-blue'), counted('users', TERMS.users, '활성사용자수', '명', 'tone-green'), counted('queries', TERMS.turns, '질의수', '회', 'tone-grey'),
    { key: 'queries_per_session', label: `${TERMS.sessions}당 평균 ${TERMS.turns}`, excel: '세션당 평균 질의수', unit: '회', format: fixed2 }, { key: 'avg_session_seconds', label: `평균 ${TERMS.sessions} 시간`, excel: '평균 세션시간(초)', format: minutes, csv: (value) => String(Math.round(value)) },
    { ...counted('refusals', '답변거절 수', '답변거절수', '회', 'tone-red'), detail: (row) => refusedText(row.refused), hint: REFUSAL_HINT }, counted('links', '링크 제공 수', '링크제공수', '회', 'tone-blue'), counted('clicks', TERMS.clicks, '클릭수', '회', 'tone-yellow'),
    { key: 'click_rate', label: '클릭률', excel: '클릭률', format: (value) => value == null ? '-' : `${value.toFixed(2)}%` },
  ];
  for (const column of STAT_COLUMNS) column.hint ??= METRIC_HINTS[column.key];
  const statColumn = Object.fromEntries(STAT_COLUMNS.map((column) => [column.key, column]));
  const LEAD = ['day', 'weekday'];
  // 묶음별 카드 행 · 추이 선 · 요일 막대 · 표 열. 표는 전체 묶음이 11열 전부이고, CSV는 묶음과 무관하게 11열이다.
  // 아래 작은 차트(규모가 다른 둘째 지표 — 같은 x축). 비율은 일별 값으로 계산해 %로, 수는 그대로.
  const BELOW = {
    users: { label: TERMS.users, format: num, value: (row) => row.users, tone: 'tone-green' },
    refusal_rate: { label: '답변거절률', format: percent, value: (row) => (row.queries ? row.refusals / row.queries * 100 : null), tone: 'tone-red', hint: REFUSAL_HINT },
    click_rate: { label: '클릭률', format: percent, value: (row) => row.click_rate, tone: 'tone-yellow' },
  };
  const STAT_VIEWS = {
    all: { cards: [['sessions', 'queries', 'links'], ['users', 'queries_per_session', 'clicks']], series: ['sessions', 'queries'], columns: STAT_COLUMNS.map((column) => column.key) },
    session: { cards: [['sessions', 'users', 'avg_session_seconds']], series: ['sessions'], below: 'users', columns: [...LEAD, 'sessions', 'users', 'avg_session_seconds'],
      weekday: { key: 'sessions', title: '요일별 평균 세션 수', insight: '세션이 가장 많이 발생된 요일' } },
    query: { cards: [['queries', 'queries_per_session', 'refusals']], series: ['queries'], below: 'refusal_rate', columns: [...LEAD, 'queries', 'queries_per_session', 'refusals'],
      weekday: { key: 'queries', title: '요일별 평균 질의 수', insight: '질의가 가장 많은 요일' } },
    click: { note: '클릭은 클릭한 시각의 날짜로 세고 같은 링크를 다시 눌러도 셉니다 — 그래서 클릭률이 100%를 넘을 수 있습니다.',
      cards: [['links', 'clicks', 'click_rate']], series: ['links'], below: 'click_rate', columns: [...LEAD, 'links', 'clicks', 'click_rate'],
      weekday: { key: 'clicks', title: '요일별 평균 클릭 수', insight: '클릭이 가장 많이 일어난 요일' } },
  };

  /** 기간 바(대시보드·통계 공용, name = 'dashboard' | 'statistics'). 프리셋은 presetRange(오늘 포함)로 날짜 칸을
   *  채우고, '직접'은 기간 표시 대신 날짜 칸을 연다(crema의 기간 버튼 → 달력 패널 자리). */
  function setPeriod(name, range) {
    for (const button of $(`${name}-filters`).querySelectorAll('[data-range]')) button.setAttribute('aria-pressed', String(button.dataset.range === range));
    syncSegmentSelect($(`${name}-filters`).querySelector('.crema-segment'));
    $(`${name}-custom`).hidden = range !== 'custom';
    $(`${name}-range`).parentElement.hidden = range === 'custom';
    if (range === 'custom') return;
    [$(`${name}-since`).value, $(`${name}-until`).value] = presetRange(range);
  }
  const PERIOD_TABS = ['dashboard', 'statistics'];
  /** 기간 옮기기(대시보드 ↔ 통계) — 같은 프리셋이면 그 버튼을, '직접'이면 날짜 칸 값을 그대로. */
  function sharePeriod(from, to) {
    const range = $(`${from}-filters`).querySelector('[data-range][aria-pressed="true"]').dataset.range;
    setPeriod(to, range);
    if (range === 'custom') for (const end of ['since', 'until']) $(`${to}-${end}`).value = $(`${from}-${end}`).value;
  }
  /** 통계에서 대화로 — 그 기간(통계 기간이나 하루) + 조건. 대화 기간은 세션 갱신 시각 기준이라 통계 수와 조금 다를 수 있다. */
  const openPeriodSessions = (period, filters = {}) => openSessions({ since: period.since, until: period.until, ...filters });
  /** 통계의 한 묶음을 대시보드와 같은 기간으로 연다(대시보드 클릭 수 카드). */
  function openStatistics(view) {
    setView('statistics', view);
    state.periodFrom = 'dashboard';
    switchTab('statistics');
  }
  /** 기간 표시 문구(기간 바의 '기간 YYYY-MM-DD ~ YYYY-MM-DD'). */
  const showPeriod = (name) => { $(`${name}-range`).textContent = `${$(`${name}-since`).value} ~ ${$(`${name}-until`).value}`; };

  function loadStatistics() {
    const params = new URLSearchParams({ since: $('statistics-since').value, until: $('statistics-until').value, ...scope() });
    showPeriod('statistics');
    // 받기 전엔 이전 기간 데이터를 버린다 — 실패하면 화면은 오류인데 이전 기간 표가 내려받히면 안 된다.
    state.statistics = null;
    if ($('statistics').childElementCount) $('statistics').classList.add('is-loading');
    else $('statistics').replaceChildren(el('div', 'placeholder', '통계를 불러오는 중…'));
    return request('statistics', `/admin/api/stats?${params}`, (data) => { state.statistics = data; renderStatistics(); })
      .finally(() => { if (!pending.has('statistics')) $('statistics').classList.remove('is-loading'); });
  }

  /** crema SummaryCard: 라벨 · 큰 숫자+단위 · 보조 문구(세부가 있는 열은 세부, 아니면 기간). */
  function cremaCard(key, summary, range) {
    const column = statColumn[key], value = summary[key];
    return cardBox(column.label, column.format(value), value == null ? '' : column.unit, range, column.detail?.(summary), column.hint);
  }
  /** 카드 한 장 — 라벨 · 값(+단위) · 보조 문구(세부가 있으면 세부, 아니면 기간). */
  function cardBox(label, text, unit, range, detail, hint) {
    const card = el('div', 'crema-card'), figure = el('div', 'crema-value'), name = el('span', 'crema-label', label);
    if (hint) name.append(infoMark(hint));
    figure.append(el('strong', null, text));
    if (unit) figure.append(el('span', null, unit));
    card.append(name, figure, detail ? el('span', 'crema-sub crema-detail', detail) : el('span', 'crema-sub', range));
    return card;
  }

  /** RBTI 묶음 — 기간 합계만(일별 표·엑셀 11열 고객 양식에는 넣지 않는다). 유형은 서버가 데이터에 나온 코드만,
   *  많은 순으로 준다. 적용률 = RBTI가 적용된 질의 ÷ 기간 질의수. */
  function rbtiBlocks(data, range) {
    const applied = data.rbti.reduce((sum, row) => sum + row.turns, 0), queries = data.summary.queries;
    const percentOf = (part, whole) => (whole ? `${(part / whole * 100).toFixed(1)}%` : '-');
    const cards = el('div', 'crema-cards'), line = el('div', 'crema-row');
    line.append(cardBox('RBTI 적용률', percentOf(applied, queries), '', range, `${num(applied)} / ${num(queries)}질의`, METRIC_HINTS.rbti_rate), cardBox(`RBTI 적용 ${TERMS.turns}`, num(applied), '회', range));
    cards.append(line);
    const bars = charts.stackedBars({
      title: `RBTI 유형별 ${TERMS.turns}`, categories: data.rbti.map((row) => row.rbti), tick: String, axisTitle: 'RBTI 유형', unit: 'count', format: num,
      onPick: (code) => openPeriodSessions(data.period, { rbti: code }),
      series: [{ label: TERMS.turns, className: 'tone-blue', values: data.rbti.map((row) => row.turns) }],
    });
    bars.classList.add('crema-bars');
    // 비율 열은 따로 키를 둔다 — 같은 키면 머리 정렬 표시가 두 열에 함께 붙는다.
    const rows = data.rbti.map((row) => ({ ...row, name: state.rbtiNames[row.rbti] ?? '-', share: applied ? row.turns / applied : null }));
    const shareText = (share) => (share == null ? '-' : `${(share * 100).toFixed(1)}%`);
    const columns = [{ key: 'rbti', label: 'RBTI 유형' }, { key: 'name', label: '유형 이름' }, { key: 'turns', label: TERMS.turns, format: num },
      { key: 'users', label: '회원 수', format: num, hint: METRIC_HINTS.rbti_users }, { key: 'share', label: '적용 질의 중 비율', format: shareText }];
    const grid = table(columns, rows, (row) => openPeriodSessions(data.period, { rbti: row.rbti }), {});
    const wrap = el('div', 'bleed');  // 회원·관리자 표와 같은 목록 표(일별 표의 날짜·요일 열 서식은 안 맞는다)
    wrap.append(grid);
    const head = tableHead(`${num(rows.length)}개 유형 · 행을 누르면 그 유형의 대화`, () => saveCsv(`rbti_${data.period.since}_${data.period.until}.csv`,
      [columns.map((column) => column.label), ...rows.map((row) => [row.rbti, row.name, row.turns, row.users, row.share == null ? '' : shareText(row.share)])]));
    return [cards, bars, ...(data.rbti.length ? [head, wrap] : [])];
  }

  /** 피드백 묶음 — 좋아요·싫어요 수와 좋아요 비율, 일별 막대(평가 시각 KST), 의견이 달린 최근 피드백.
   *  일별 11열 표·엑셀(고객 양식)에는 넣지 않는다. 의견 행을 누르면 그 대화를 연다. */
  function feedbackBlocks(data, range) {
    const byDay = new Map(data.feedback.daily.map((row) => [row.day, row]));
    const days = data.daily.map((row) => row.day).reverse();
    const likes = data.feedback.daily.reduce((sum, row) => sum + row.likes, 0), dislikes = data.feedback.daily.reduce((sum, row) => sum + row.dislikes, 0);
    const cards = el('div', 'crema-cards'), line = el('div', 'crema-row');
    line.append(cardBox('좋아요', num(likes), '회', range), cardBox('싫어요', num(dislikes), '회', range),
      cardBox('좋아요 비율', likes + dislikes ? `${(likes / (likes + dislikes) * 100).toFixed(1)}%` : '-', '', range, `평가 ${num(likes + dislikes)}건 중`));
    cards.append(line);
    const bars = charts.stackedBars({
      title: '일별 피드백', categories: days, unit: 'count', format: num, onPick: (day) => openPeriodSessions({ since: day, until: day }),
      series: [['likes', 'tone-blue'], ['dislikes', 'tone-grey']].map(([key, className]) => ({ label: key === 'likes' ? FEEDBACK_LABELS.up : FEEDBACK_LABELS.down, className, values: days.map((day) => byDay.get(day)?.[key] ?? 0) })),
    });
    bars.classList.add('crema-bars');
    // 일별 좋아요·싫어요 CSV(의견이 없어도 늘 받을 수 있다) — 날짜는 통계 일별 표와 같은 최신순.
    const daily = tableHead(`${num(days.length)}일 · 좋아요·싫어요`, () => saveCsv(`feedback_daily_${data.period.since}_${data.period.until}.csv`,
      [['날짜', FEEDBACK_LABELS.up, FEEDBACK_LABELS.down], ...[...days].reverse().map((day) => [day, byDay.get(day)?.likes ?? 0, byDay.get(day)?.dislikes ?? 0])]));
    const comments = data.feedback.comments, head = el('div', 'section-head');
    head.append(el('h3', null, '의견이 달린 피드백'));
    if (!comments.length) return [cards, daily, bars, head, el('div', 'placeholder', '이 기간에 의견이 달린 피드백이 없습니다.')];
    const list = table([
      { key: 'updated_at', label: '평가 시각', format: when }, { key: 'rating', label: '평가', format: (v) => el('em', `feedback-${v}`, FEEDBACK_LABELS[v] ?? v) },
      { key: 'question', label: '질문', format: day }, { key: 'comment', label: '의견' },
    ], comments, (row) => { switchTab('sessions'); selectSession({ id: row.session_id, user_id: row.user_id }); });
    const wrap = el('div', 'bleed');
    wrap.append(list);
    const tools = tableHead(`최근 ${num(comments.length)}건 · 행을 누르면 그 대화`, () => saveCsv(`feedback_${data.period.since}_${data.period.until}.csv`,
      [['평가 시각', '평가', '질문', '의견'], ...comments.map((row) => [stamp(row.updated_at), FEEDBACK_LABELS[row.rating] ?? row.rating, row.question ?? '', row.comment])]));
    return [cards, daily, bars, head, tools, wrap];
  }

  function renderStatistics() {
    const data = state.statistics, view = STAT_VIEWS[state.statisticsView];
    if (!data) return;
    const range = `${data.period.since} ~ ${data.period.until}`;
    if (state.statisticsView === 'rbti') { $('statistics').replaceChildren(...rbtiBlocks(data, range)); return; }
    if (state.statisticsView === 'feedback') { $('statistics').replaceChildren(...feedbackBlocks(data, range)); return; }
    const cards = el('div', 'crema-cards');
    for (const row of view.cards) { const line = el('div', 'crema-row'); line.append(...row.map((key) => cremaCard(key, data.summary, range))); cards.append(line); }
    const oldestFirst = [...data.daily].reverse();
    const days = oldestFirst.map((row) => row.day), openDay = (day) => openPeriodSessions({ since: day, until: day });
    const trend = charts.lines({
      title: `${range} 기준`, categories: days, format: num, onPick: openDay,
      // 범례엔 기간 합계를 붙이고(legend), 툴팁·표는 계열명만(label) — 합계가 칸 값 옆에 붙으면 '12 세션수 2,322'로 읽힌다.
      series: view.series.map((key) => ({ label: statColumn[key].label, legend: `${statColumn[key].label} ${num(data.summary[key])}`, className: statColumn[key].tone, values: oldestFirst.map((row) => row[key]) })),
    });
    // 규모가 다른 둘째 지표는 한 차트에 겹치지 않고 아래 작은 차트로(같은 x축 — 날짜 칸이 위아래로 맞는다).
    const below = view.below && BELOW[view.below];
    const lower = below && charts.lines({
      title: below.label, categories: days, format: below.format, height: 90, onPick: openDay,
      series: [{ label: below.label, className: below.tone, values: oldestFirst.map(below.value) }],
    });
    const blocks = [...(view.note ? [el('p', 'crema-note', view.note)] : []), cards, trend, ...(lower ? [lower] : [])];
    if (view.weekday) {
      const { key, title, insight } = view.weekday, column = statColumn[key];
      const bars = charts.stackedBars({
        title, categories: data.weekday.map((row) => row.weekday), tick: String, axisTitle: '요일', format: average,
        series: [{ label: `평균 ${column.label}`, className: 'tone-blue', values: data.weekday.map((row) => row[key]) }],
      });
      bars.classList.add('crema-bars');
      blocks.push(bars);
      const top = data.weekday.reduce((best, row) => (row[key] ?? 0) > (best?.[key] ?? 0) ? row : best, null);
      const card = el('div', 'crema-insight'), metric = el('span', 'crema-metric');
      metric.append(el('span', null, `평균 ${column.label}`), el('b', null, top ? `${average(top[key])}${column.unit}` : '-'));
      card.append(el('span', null, insight), el('strong', null, top ? `${top.weekday}요일` : '-'), metric);
      blocks.push(card);
    }
    const head = tableHead(`${num(data.daily.length)}일 · 행을 누르면 그날 대화`, downloadStatistics);
    const gap = el('p', 'crema-note', '대화 목록의 날짜는 대화의 마지막 갱신 시각 기준이라, 그날 대화 수가 이 표의 수와 조금 다를 수 있습니다.');
    // 맨 위 고정 행 = 기간 전체(합계가 아니라 기간으로 다시 센 요약 — 활성 사용자는 기간 고유 수). 정렬해도 머리에 남는다.
    const grid = table(view.columns.map((key) => statColumn[key]), data.daily, (row) => openPeriodSessions({ since: row.day, until: row.day }), {},
      { ...data.summary, day: '기간 전체', weekday: '' });
    grid.classList.add('crema-table', 'bleed');
    $('statistics').replaceChildren(...blocks, head, gap, grid);
  }

  /** 표 머리 줄 — 설명 글자 + CSV 다운로드 버튼(통계 일별·RBTI·피드백 공용). */
  function tableHead(text, download) {
    const head = el('div', 'crema-table-head'), button = el('button', 'crema-download');
    button.type = 'button';
    button.append($('download-icon').content.firstElementChild.cloneNode(true), document.createTextNode('CSV 다운로드'));
    button.onclick = download;
    head.append(el('span', null, text), button);
    return head;
  }
  /** CSV 다운로드 = 일별 11열(고객 양식 머리·날짜 YYYY.MM.DD, 최신순) + 맨 위 '기간 전체' 행(화면 고정 행과 같은 값).
   *  시간은 초(수), 건수는 원값 — 엑셀이 수로 읽게. */
  function downloadStatistics() {
    const data = state.statistics;
    if (!data) return;
    // 값이 없는 칸(분모 0)은 빈 칸이다 — 화면의 '-'는 선두 '-'라 셀 규칙이 '로 감싸 엑셀에 '-로 보인다.
    const line = (row) => STAT_COLUMNS.map((column) => (row[column.key] == null ? '' : (column.csv || column.format)(row[column.key])));
    saveCsv(`statistics_${data.period.since}_${data.period.until}.csv`, [
      STAT_COLUMNS.map((column) => column.excel),
      line({ ...data.summary, day: null, weekday: null }).map((cell, i) => (i === 0 ? '기간 전체' : cell)),
      ...data.daily.map(line),
    ]);
  }


  const manage = initManage({ api, el, table, valueText, state, openDialog, showRecord, reload: () => { state.preview = null; loaders[state.tab](); }, onCreated: starterCreated, slotLabel, generateStatus: GENERATE_STATUS, errorText, chanceText, slotProbability, todayPreview, pins: PINS });
  /** 직접 등록 성공 — 패널을 닫고 알림, 다시 읽은 화면에서 새 질문(캘린더 막대)을 잠깐 강조한다. */
  function starterCreated(created) {
    closePanel();
    state.preview = null;
    showToast('첫 화면 질문을 등록했습니다.');
    state.flashId = created.id;
    loaders[state.tab]();
  }

  // 기간 바: 프리셋은 누르면 바로 조회, '직접'은 날짜 칸의 조회 버튼으로. 고른 쪽을 기억해 다른 화면이 이어 쓴다.
  for (const [name, load] of [['dashboard', loadDashboard], ['statistics', loadStatistics]]) {
    for (const button of $(`${name}-filters`).querySelectorAll('[data-range]')) button.onclick = () => {
      setPeriod(name, button.dataset.range);
      state.periodFrom = name;
      if (button.dataset.range !== 'custom') { $('app-error').hidden = true; load(); }
    };
    $(`${name}-filters`).onsubmit = (event) => { event.preventDefault(); state.periodFrom = name; $('app-error').hidden = true; load(); };
  }
  // 화면 안 보기 묶음(통계 지표 묶음 · 초기 질문 풀/인기 질문) — state[탭 + 'View'], 버튼 묶음 #탭-views.
  const VIEWS = { statistics: { initial: 'all', show: () => renderStatistics() }, starters: { initial: 'calendar', show: () => loadStarters() } };
  /** 보기 고르기 — 버튼 묶음에 없는 값(손으로 고친 주소)은 첫 보기로. */
  function setView(tab, view) {
    const buttons = [...$(`${tab}-views`).children];
    state[`${tab}View`] = buttons.some((button) => button.dataset.view === view) ? view : VIEWS[tab].initial;
    for (const button of buttons) button.setAttribute('aria-pressed', String(button.dataset.view === state[`${tab}View`]));
  }
  /** 보기 전환은 뒤로가기의 한 걸음이다 — 지금 주소를 한 번 더 쌓고(push), 이어지는 writeHash(replace)가 그
   *  위를 새 보기로 바꾼다. 조건·페이지 변경은 replace만(기록이 쌓이지 않게). */
  const pushHistory = () => history.pushState(history.state, '', location.hash);
  for (const tab of Object.keys(VIEWS)) for (const button of $(`${tab}-views`).children) button.onclick = () => {
    if (state[`${tab}View`] !== button.dataset.view) pushHistory();
    setView(tab, button.dataset.view);
    writeHash();
    VIEWS[tab].show();
  };
  $('record-close').onclick = closePanel;
  $('record-dialog').onclose = () => {
    state.pendingPin = null;  // 패널을 닫으면 하던 고정 확인도 끝
    $('record-view').replaceChildren();
    $('record-actions').replaceChildren();
    for (const row of document.querySelectorAll('tr[aria-current]')) row.removeAttribute('aria-current');
  };
  // 비모달 패널은 Esc를 스스로 받지 않는다 — 문서에서 받아 닫는다.
  // Esc: 기간 선택 중이면 선택만 취소, 아니면 패널을 닫는다.
  // 실제 Esc는 keydown 뒤에 브라우저 '닫기 요청'이 따른다 — 여기서 확인 대화상자를 열면 그 요청이 방금 연 모달을
  // 바로 취소한다. 처리한 Esc는 preventDefault로 닫기 요청을 막는다.
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || $('confirm-dialog').open) return;  // 확인 대화상자는 스스로 Esc를 받는다
    if (!infoPop.hidden) { event.preventDefault(); closeInfo(); return; }  // 열린 설명이 먼저 닫힌다
    if (!state.range && !$('record-dialog').open) return;  // 처리할 것이 없으면 브라우저 기본 동작 그대로
    event.preventDefault();
    if (state.range) clearRange(); else closePanel();
  });
  $('users-filters').onsubmit = (event) => { event.preventDefault(); applyQuery('users'); $('app-error').hidden = true; loadUsers(); };
  $('starters-filters').onsubmit = (event) => { event.preventDefault(); applyQuery('starters'); $('app-error').hidden = true; loadStarters(); };
  // 검색어는 입력하는 대로 반영한다(같은 줄의 선택 상자처럼) — 타자 사이 SEARCH_DEBOUNCE_MS 동안 멈추면 조회.
  const SEARCH_DEBOUNCE_MS = 300;
  for (const config of Object.values(queryForms)) {
    if (!config.fields.q) continue;
    let timer;
    $(config.fields.q).addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => $(config.form).requestSubmit(), SEARCH_DEBOUNCE_MS);
    });
  }
  // 좁은 화면은 검색·노출 상태만 두고 나머지 선택 상자를 '필터 더 보기'로 접는다(넓은 화면은 한 줄에 다 보인다).
  function syncMoreFilters() {
    const extra = ['source', 'term', 'slot'].filter((key) => state.applied.starters?.[key]).length;
    $('starters-more').textContent = $('starters-filters').classList.contains('expanded') ? '필터 접기' : `필터 더 보기${extra ? ` (${num(extra)})` : ''}`;
  }
  $('starters-more').onclick = () => { $('starters-filters').classList.toggle('expanded'); syncMoreFilters(); };
  // 검색칸(type=search): Enter로 검색, 지우기(✕)로 비우면 곧바로 전체를 다시 읽는다. 대화 폼은 날짜 칸이 있어
  // 브라우저의 Enter 암묵 제출이 막히므로 Enter도 여기서 받는다.
  for (const [input, form] of [['q', 'filters'], ['users-q', 'users-filters'], ['starters-q', 'starters-filters']]) {
    $(input).addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $(form).requestSubmit(); } });
    $(input).addEventListener('search', () => { if (!$(input).value) $(form).requestSubmit(); });
  }

  const loaders = { dashboard: loadDashboard, statistics: loadStatistics, sessions: loadSessions, users: loadUsers, starters: loadStarters, audit: loadAudit };
  // 감사 로그 필터 — 작업 종류 세그먼트·계정 선택, 바꾸면 첫 페이지부터 다시 읽는다.
  $('audit-filters').onsubmit = (event) => { event.preventDefault(); applyQuery('audit'); loadAudit(); };
  $('audit-actor').onchange = () => $('audit-filters').requestSubmit();
  // 값 세그먼트(숨은 칸 data-segment — 피드백·답변 상태·작업 종류): 누르면 그 값으로 바로 조회한다.
  for (const input of document.querySelectorAll('input[type=hidden][data-segment]')) for (const button of $(input.dataset.segment).children) button.onclick = () => {
    press($(input.dataset.segment), button);
    input.value = button.dataset.value;
    input.form.requestSubmit();
  };
  // 기간 세그먼트(대화 갱신 시각·감사 기록 시각): 어드민 시간대 날짜 칸을 채우고 바로 조회한다. '직접'은 날짜 칸을 연다.
  for (const [group, since, until] of [['sessions-range', 'since', 'until'], ['audit-range', 'audit-since', 'audit-until']]) for (const button of $(group).children) button.onclick = () => {
    press($(group), button);
    const range = button.dataset.range;
    $(since).closest('.crema-custom').hidden = range !== 'custom';
    if (range === 'custom') return;
    [$(since).value, $(until).value] = range ? presetRange(range) : ['', ''];
    $(since).form.requestSubmit();
  };
  // 목록 필터 선택 상자 — 바꾸면 바로 다시 읽는다.
  for (const [select, form] of [['users-rbti', 'users-filters'], ['users-nick', 'users-filters'], ['sessions-rbti', 'filters'], ['starters-status', 'starters-filters'], ['starters-source', 'starters-filters'], ['starters-term', 'starters-filters'], ['starters-slot', 'starters-filters']]) $(select).onchange = () => $(form).requestSubmit();

  // 화면별 최소 역할 — 메뉴를 숨기는 것만으로는 주소(#audit)로 직접 들어오는 길이 남는다. 판정은 서버(403)가
  // 하지만, 열 수 없는 화면을 그리고 오류를 띄우는 대신 대시보드로 돌려보낸다.
  const TAB_ROLES = { audit: 'owner' };
  async function switchTab(tab) {
    // 패널은 연 화면의 행을 보여 준다 — 화면을 떠나면 닫는다(저장 안 한 변경이 있을 때만 묻고 기다린다).
    if (panelDirty() && !(await confirmLeave())) return;
    $('record-dialog').close();
    closeInfo();  // 앞 화면의 (i) 설명이 새 화면을 가리지 않게
    if (TAB_ROLES[tab] && !hasRole(TAB_ROLES[tab])) tab = 'dashboard';
    state.tab = tab;
    for (const name of Object.keys(loaders)) $(name + '-pane').hidden = tab !== name;
    // 대시보드 ↔ 통계는 마지막으로 고른 기간을 이어 쓴다.
    if (PERIOD_TABS.includes(tab) && state.periodFrom && state.periodFrom !== tab) sharePeriod(state.periodFrom, tab);
    if (tab === 'sessions') showDetail(false);
    $('app').classList.remove('nav-open');
    $('menu-toggle').setAttribute('aria-expanded', 'false');
    for (const name of Object.keys(loaders)) $('tab-' + name).setAttribute('aria-pressed', String(name === tab));
    $('app-error').hidden = true;
    document.title = `${$('tab-' + tab).textContent} · 크레마 AI 관리자`;
    writeHash();
    return loaders[tab]();
  }

  // ── 주소(해시)에 화면 상태 — 새로고침·링크 공유·뒤로가기가 같은 화면으로 돌아온다.
  // #탭?조회조건&page=&sort=&dir=&view=&s=세션&u=회원. 조회 조건 키는 queryForms 필드가 정본이다.
  const SORT_KEYS = {
    sessions: () => [...$('sessions-sort').options].map((option) => option.value).filter(Boolean),
    users: () => USER_COLUMNS.filter((column) => column.sort).map((column) => column.key),
    starters: () => STARTER_COLUMNS.filter((column) => column.sort).map((column) => column.key),
  };
  const PAGE_KEYS = { sessions: 'page', users: 'usersPage', starters: 'startersPage', audit: 'auditPage' };
  function writeHash() {
    const tab = state.tab;
    // 조건은 기본값과 다를 때만 주소에 남긴다(노출 상태 기본 '지금 노출 중'은 주소에 없다 — readHash가 기본으로 채운다).
    const form = queryForms[tab], applied = Object.entries(form ? state.applied[tab] : {}).filter(([key, value]) => value !== $(form.fields[key]).dataset.default);
    const params = new URLSearchParams(applied);
    if (state[PAGE_KEYS[tab]]) params.set('page', state[PAGE_KEYS[tab]]);
    if (state.sort[tab]) { params.set('sort', state.sort[tab].key); params.set('dir', state.sort[tab].dir); }
    if (VIEWS[tab] && state[`${tab}View`] !== VIEWS[tab].initial) params.set('view', state[`${tab}View`]);
    if (SCOPED_TABS.includes(tab) && state.includeInternal) params.set('internal', '1');
    if (tab === 'starters' && state.startersView === 'calendar' && state.calMonth && state.calMonth !== state.calendar?.today.slice(0, 7)) params.set('month', state.calMonth);
    if (tab === 'sessions' && state.currentSession) { params.set('s', state.currentSession.id); if (state.currentSession.user_id) params.set('u', state.currentSession.user_id); }
    const hash = `#${tab}${[...params].length ? `?${params}` : ''}`;
    if (location.hash !== hash) history.replaceState(history.state, '', hash);  // 상태 객체(상세 표시)는 그대로
  }
  /** 해시 → 화면 상태(탭·조건 칸·세그먼트 표시·페이지·정렬·보기·열린 세션). 모르는 탭이면 그대로 둔다. */
  function readHash() {
    const [tab, query = ''] = location.hash.slice(1).split('?');
    if (!loaders[tab]) return;
    const params = new URLSearchParams(query);
    state.tab = tab;
    const form = queryForms[tab];
    if (form) {
      for (const [key, id] of Object.entries(form.fields)) {
        const input = $(id), value = params.get(key) ?? input.dataset.default ?? '';
        // 선택지가 아직 없는 값(데이터에서 오는 RBTI 코드)은 먼저 선택지를 만든다.
        if (input.tagName === 'SELECT' && value && ![...input.options].some((option) => option.value === value)) input.append(new Option(value, value));
        input.value = value;
      }
      syncSegments(tab);
      applyQuery(tab);
    }
    if (PAGE_KEYS[tab]) state[PAGE_KEYS[tab]] = Math.max(0, Number(params.get('page')) || 0);
    // 손으로 고친 주소의 모르는 값은 기본으로 — 정렬은 그 화면의 정렬 가능 열(대화는 정렬 선택지), 보기는 묶음 버튼.
    const sort = params.get('sort') ? { key: params.get('sort'), dir: params.get('dir') === 'asc' ? 'asc' : 'desc' } : undefined;
    state.sort[tab] = sort && SORT_KEYS[tab]?.().includes(tab === 'sessions' ? `${sort.key}:${sort.dir}` : sort.key) ? sort : undefined;
    if (VIEWS[tab]) setView(tab, params.get('view'));
    if (SCOPED_TABS.includes(tab)) { state.includeInternal = params.get('internal') === '1'; renderInternalToggles(); }
    if (tab === 'starters') state.calMonth = /^\d{4}-(0[1-9]|1[0-2])$/.test(params.get('month') ?? '') ? params.get('month') : null;
    if (tab === 'sessions') {
      state.currentSession = params.get('s') ? { id: params.get('s'), user_id: params.get('u') ?? '' } : null;
      $('sessions-sort').value = state.sort.sessions ? `${state.sort.sessions.key}:${state.sort.sessions.dir}` : '';
    }
  }
  /** 해시가 가리키는 화면 열기 — 세션이 지정돼 있으면 목록과 함께 그 상세도 연다. */
  async function openFromHash() {
    // 저장 안 한 변경을 두고 떠나지 않기로 하면 상태를 바꾸기 전에 멈추고 주소를 지금 화면으로 되돌린다.
    if (panelDirty() && !(await confirmLeave())) { writeHash(); return; }
    readHash();
    const opened = switchTab(state.tab);
    if (state.tab === 'sessions' && state.currentSession) selectSession(state.currentSession, { reveal: !!history.state?.detail });
    return opened;
  }
  // 화면 이동은 해시가 바뀔 때 한 번(hashchange — 뒤로가기·붙여 넣은 링크 공통). popstate는 해시가 그대로인
  // 좁은 화면의 목록 ↔ 상세 기록 칸만 맡는다 — 둘 다 openFromHash를 부르면 해시 뒤로가기에서 두 번 돈다.
  window.addEventListener('popstate', () => {
    const shown = $('sessions-pane').classList.contains('has-selection');
    if (shown && !history.state?.detail) showDetail(false);
    else if (!shown && history.state?.detail && state.currentSession) showDetail(true);
  });
  window.addEventListener('hashchange', () => { closeInfo(); if (state.role) openFromHash(); });

  $('gate').onsubmit = async (e) => {
    e.preventDefault();
    // 빈 칸은 서버에 묻기 전에 그 칸 아래 오류 줄로 알린다(브라우저 말풍선 대신 — novalidate).
    for (const input of [$('username'), $('password')]) input.removeAttribute('aria-invalid');
    const empty = [['username', '계정명을 입력하세요.'], ['password', '비밀번호를 입력하세요.']].find(([id]) => !$(id).value.trim());
    if (empty) {
      $('gate-error').textContent = empty[1];
      $('gate-error').hidden = false;
      $(empty[0]).setAttribute('aria-invalid', 'true');
      $(empty[0]).focus();
      return;
    }
    const button = $('gate').querySelector('button');
    button.disabled = true;
    $('gate-error').hidden = true;
    $('gate').classList.remove('failed');
    try {
      await api('/admin/api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: $('username').value.trim(), password: $('password').value }),
      });
      $('password').value = '';
      await start();
    } catch (error) {
      const retry = Number(error.response?.headers.get('Retry-After'));
      $('gate-error').textContent = error.status === 429 && retry ? `${error.message} ${num(Math.ceil(retry / 60))}분(${num(retry)}초) 뒤 다시 시도하세요.` : error.status ? error.message : '서버에 연결하지 못했습니다. 다시 시도하세요.';
      $('gate-error').hidden = false;
      $('gate').classList.add('failed');  // 입력칸 테두리를 오류색으로(Login.tsx)
    } finally { button.disabled = false; }
  };

  // Caps Lock 안내 — 비밀번호 칸에서 키를 누를 때마다 상태를 본다.
  for (const type of ['keydown', 'keyup']) $('password').addEventListener(type, (event) => { $('gate-caps').hidden = !event.getModifierState?.('CapsLock'); });
  $('logout').onclick = async () => {
    $('logout').disabled = true;
    try { await api('/admin/api/logout', { method: 'POST' }); location.replace('/admin'); }
    catch (error) { showError(`로그아웃 요청에 실패했습니다: ${errorText(error, '다시 시도해 주세요.')}`); $('logout').disabled = false; }
  };
  $('password-change').onclick = () => manage.openPasswordChange();

  // 조건을 바꾸면 이전 대화가 상세에 남지 않게 선택을 비운다 — 새 목록의 첫 대화가 열린다(페이지 이동은 유지).
  $('filters').onsubmit = (e) => { e.preventDefault(); applyQuery('sessions'); state.currentSession = null; $('app-error').hidden = true; loadSessions(); };
  /** 다른 화면에서 조건을 걸어 목록 화면을 연다 — 다른 조건은 전부 풀고 filters(폼 필드 키 → 값)만 건다.
   *  대화(회원의 대화 보기·대시보드 카드)는 첫 대화가, 회원(대화 상세의 회원 보기)은 그 회원 패널이 열린다. */
  function openList(name, filters) {
    resetQuery(name, false);
    for (const [key, value] of Object.entries(filters)) $(queryForms[name].fields[key]).value = value;
    syncSegments(name);
    applyQuery(name);
    switchTab(name);
  }
  // 다른 화면에서 여는 목록은 기본 정렬(최근순)부터 — 앞서 고른 정렬이 '그날 대화'의 순서를 바꾸지 않게.
  const openSessions = (filters) => { state.currentSession = null; state.sort.sessions = undefined; $('sessions-sort').value = ''; openList('sessions', filters); };
  const openMember = (userNo) => { state.pendingMember = String(userNo); openList('users', { q: userNo }); };
  // 대화 정렬 — 값 'key:dir'(빈 값 = 최근순). 바꾸면 첫 페이지부터, 새 목록의 첫 대화를 연다.
  $('sessions-sort').onchange = () => {
    const [key, dir] = $('sessions-sort').value.split(':');
    state.sort.sessions = key ? { key, dir } : undefined;
    state.page = 0;
    state.currentSession = null;
    loadSessions();
  };
  for (const [id, download] of [['sessions-csv', downloadSessions], ['users-csv', downloadUsers], ['audit-csv', downloadAudit]]) {
    $(id).prepend($('download-icon').content.firstElementChild.cloneNode(true));
    $(id).onclick = download;
  }
  $('menu-toggle').onclick = () => {
    const open = $('app').classList.toggle('nav-open');
    $('menu-toggle').setAttribute('aria-expanded', String(open));
  };
  // 서랍 바깥은 덮개(#nav-scrim)가 탭을 받는다 — 아래 요소로 클릭이 새지 않고 서랍만 닫힌다(crema RootLayout).
  $('nav-scrim').onclick = () => { $('app').classList.remove('nav-open'); $('menu-toggle').setAttribute('aria-expanded', 'false'); };
  /** 새로고침 = 지금 화면을 다시 읽는다(활성 메뉴를 다시 누를 때). 대화는 열어 둔 상세의 펼침·스크롤을 지킨다. */
  function refresh() {
    $('app-error').hidden = true;
    loadOverview();
    loaders[state.tab]();
    if (state.tab === 'sessions' && state.currentSession) selectSession(state.currentSession, {preservePosition: true});
  }
  for (const tab of Object.keys(loaders)) $('tab-' + tab).onclick = () => (tab === state.tab ? refresh() : switchTab(tab));

  /** 세션이 있으면 역할을 받아 화면을 연다. 401이면 로그인 게이트만 보인다. */
  async function start() {
    let me;
    try { me = await api('/admin/api/me'); }
    catch (error) {
      $('gate').hidden = false;
      $('username').focus();  // 게이트가 숨은 채 로드돼 autofocus 속성은 먹지 않는다
      if (error.status !== 401) { $('gate-error').textContent = error.message; $('gate-error').hidden = false; }
      return;
    }
    state.role = me.role;
    state.roles = me.roles;
    state.username = me.username;
    state.passwordMin = me.password_min_length;
    state.changeMinBase = me.change_min_base;
    state.excludedAccounts = me.excluded_accounts;
    state.rbtiNames = me.rbti_names ?? {};
    // RBTI 필터 선택지(회원·대화 같은 함수) = 있음·없음 + 유형표 전체(/me rbti_names — rbti.persona가 정본).
    // 데이터에만 있는 코드는 목록 응답이 덧붙인다.
    for (const id of ['users-rbti', 'sessions-rbti']) addMissingOptions($(id), ['any', 'none', ...Object.keys(state.rbtiNames)], (code) => (PRESENCE[code] ? `RBTI ${PRESENCE[code]}` : rbtiLabel(code)));
    for (const id of ['dashboard-filters', 'statistics-filters', 'filters', 'users-filters', 'starters-filters', 'popular-filters']) $(id).append(Object.assign(el('span', 'internal-toggle'), { hidden: true }));
    renderInternalToggles();
    // 시간대가 정해진 뒤에야 날짜 칸 기본값(오늘 기준 프리셋)과 화면 라벨을 채울 수 있다.
    state.tz = me.timezone;
    for (const node of document.querySelectorAll('.tz')) node.textContent = state.tz.label;
    for (const group of [$('dashboard-filters').querySelector('.crema-segment'), $('statistics-filters').querySelector('.crema-segment'), $('sessions-range'), $('audit-range')]) segmentSelect(group);
    setPeriod('dashboard', '7');
    setPeriod('statistics', '7');
    $('account').replaceChildren(document.createTextNode(me.username), el('small', null, roleLabel(me.role)));
    $('toolbar-owner').hidden = me.role !== 'owner';
    $('gate').hidden = true;
    for (const id of ['sidebar', 'mobile-bar', 'main']) $(id).hidden = false;
    await Promise.all([loadOverview(), openFromHash()]);
  }
  start();
