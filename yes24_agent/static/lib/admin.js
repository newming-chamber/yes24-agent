import { initManage, roleLabel } from "/static/lib/admin_manage.js?v=12";
import { initCharts } from "/static/lib/admin_charts.js?v=8";
import { renderBody } from "/static/lib/md.js";
import { coverUrl, formatPrice, isSafeUrl, makeCoverImg, sourceCardType, sourceDomain, sourceTitle, CARD_LABELS } from "/static/lib/sources.js";

  const $ = (id) => document.getElementById(id);
  const state = { page: 0, tz: null, tab: 'dashboard', statistics: null, statisticsView: 'all', usersPage: 0, startersPage: 0, auditPage: 0, sort: {}, applied: {}, currentSession: null, listScroll: 0, username: null, role: null, roles: [], passwordMin: 0, changeMinBase: 0, starterSlots: [], pendingMember: null, periodFrom: null };
  const queryForms = {
    sessions: {form: 'filters', page: 'page', fields: {q: 'q', since: 'since', until: 'until', rating: 'rating', status: 'status', rbti: 'sessions-rbti'}},
    users: {form: 'users-filters', page: 'usersPage', fields: {q: 'users-q', rbti: 'users-rbti', nickname: 'users-nick'}},
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
  const TERMS = { turns: '질의 수', users: '활성 사용자', clicks: '클릭 수', sessions: '세션', active: '노출' };
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
    const [y, m] = localDay().split('-').map(Number), day = (date) => date.toISOString().slice(0, 10);
    return key === 'month' ? [day(new Date(Date.UTC(y, m - 1, 1))), localDay()] : [day(new Date(Date.UTC(y, m - 2, 1))), day(new Date(Date.UTC(y, m - 1, 0)))];
  }
  /** 세그먼트 버튼 묶음에서 하나만 눌림 표시. */
  const press = (group, pressed) => { for (const button of group.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button === pressed)); };
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
  const showError = (message) => { $('app-error').hidden = false; $('app-error').textContent = message; };

  async function request(key, path, render) {
    pending.get(key)?.abort();
    const controller = new AbortController();
    pending.set(key, controller);
    try {
      const data = await api(path, { signal: controller.signal });
      if (!controller.signal.aborted) render(data);
    } catch (error) {
      if (error.name !== 'AbortError' && !controller.signal.aborted) {
        showError(error.message);
        const target = $(key);
        if (target) {
          const bar = key === 'detail' ? target.querySelector('#detail-bar') : null;
          const retry = el('button', 'link-button', '다시 시도');
          retry.type = 'button';
          retry.onclick = () => { $('app-error').hidden = true; request(key, path, render); };
          target.replaceChildren(...(bar ? [bar] : []), placeholder('불러오지 못했습니다.', retry));
        }
      }
    } finally {
      if (pending.get(key) === controller) pending.delete(key);
    }
  }

  function loadOverview() {
    return request('stats', '/admin/api/overview', (o) => {
      // 용어는 통계 탭과 같다(TERMS) — 활성 사용자 = 실제로 대화한 사용자, 세션 = 대화방(질문 여러 개가 한 세션).
      // 카드를 누르면 그 목록(회원 · 전체 대화)으로 간다.
      const stats = [[`${TERMS.users} (누적)`, num(o.chat_users), '실제로 대화한 사용자 수(전체 기간) · 누르면 회원 목록', () => switchTab('users')],
        [`${TERMS.sessions} (누적)`, num(o.sessions), '대화방 수(질문 여러 개가 한 세션) · 누르면 전체 대화', () => openSessions({})],
        [`${TERMS.turns} (누적)`, num(o.turns), '누르면 전체 대화', () => openSessions({})]];
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
  const minutes = (value) => { if (value == null) return '-'; const s = Math.round(value); return `${Math.floor(s / 60)}분 ${s % 60}초`; };

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
    const sort = state.sort.sessions, params = new URLSearchParams({ ...state.applied.sessions, ...(sort ? { sort: sort.key, dir: sort.dir } : {}), page });
    $('sessions').replaceChildren(el('div', 'placeholder', '대화를 불러오는 중…'));
    return request('sessions', `/admin/api/sessions?${params}`, (data) => {
      $('page-info').textContent = rangeText(data);
      addMissingOptions($('sessions-rbti'), data.rbti_types);
      const rows = data.items.map((s) => {
        const row = el('button', 'query-row');
        row.type = 'button';
        row.dataset.id = s.id;
        row.setAttribute('aria-current', String(s.id === state.currentSession?.id));
        row.onclick = () => selectSession(s);
        // 카드 3줄(crema QueryAnalysis): ① 세션 id·배지 … 날짜 시각 ② 첫 질문 ③ '질의 n건 · 닉네임(회원번호)'.
        const top = el('span', 'q-top'), [day, time] = clockParts(s.update_time), when = el('span', 'q-time', day);
        when.append(el('small', null, time));
        top.append(el('span', 'q-id', s.id.slice(0, 8)));
        // 그 세션에서 가장 최근에 적용된 RBTI — 누르면 그 유형으로 거른다(카드 전체가 버튼이라 배지는 em + 클릭).
        if (s.rbti) { const badge = el('em', 'rbti', s.rbti); badge.title = `${s.rbti}만 보기 · 다시 누르면 해제`; badge.onclick = (event) => { event.stopPropagation(); toggleRbti('sessions', s.rbti); }; top.append(badge); }
        for (const [rating, key] of [['up', 'likes'], ['down', 'dislikes']]) if (s[key]) top.append(el('em', `feedback-${rating}`, `${FEEDBACK_LABELS[rating]} ${num(s[key])}`));
        for (const [status, n] of Object.entries(s.refused ?? {})) if (n) top.append(el('em', `status-${status}`, `${statusLabel(status)} ${num(n)}`));
        top.append(when);
        const meta = el('small', null, `질의 ${num(s.turn_count)}건 · ${s.nickname === s.user_id ? s.user_id : `${s.nickname}(${s.user_id})`}`);
        row.append(top, el('b', s.preview ? null : 'empty', s.preview || '(사용자 발화 없음)'), meta);
        row.title = s.id;
        return row;
      });
      $('sessions').replaceChildren(...(rows.length ? rows : [emptyList('sessions', '조건에 맞는 대화가 없습니다.')]));
      cremaPager($('sessions-pager'), data.total, data.page_size, page, (to) => { state.page = to; loadSessions(); $('sessions-list').scrollTop = $('sessions-pane').scrollTop = 0; });
      // 상세 칸을 비워 두지 않는다 — 아직 고른 대화가 없으면 첫 대화를 연다(좁은 화면은 목록에 머문다).
      if (!state.currentSession && data.items.length) selectSession(data.items[0], { reveal: false });
      else if (!state.currentSession) $('detail').replaceChildren(el('div', 'placeholder', '조건에 맞는 대화가 없습니다.'));
    });
  }

  /** 엑셀 다운로드 = 적용한 조건·정렬의 목록 전체(페이지를 이어 받아 UTF-8 BOM CSV로) — 대화·회원 공용.
   *  line(행, 순번, 전체 행)은 CSV 한 줄. 파일은 화면 밖으로 나가므로 시각 열 머리에 시간대를 적는다. */
  async function downloadList(button, name, path, head, line) {
    const rows = [], query = { ...state.applied[name], ...(state.sort[name] ? { sort: state.sort[name].key, dir: state.sort[name].dir } : {}) };
    button.disabled = true;
    try {
      for (let page = 0; ; page++) {
        const data = await api(`${path}?${new URLSearchParams({ ...query, page })}`);
        rows.push(...data.items);
        if (rows.length >= data.total || !data.items.length) break;
      }
      saveCsv(`${name}_${localDay()}.csv`, [head, ...rows.map((row, i) => line(row, i, rows))]);
    } catch (error) { if (error.name !== 'AbortError') showError(`다운로드 실패: ${error.message}`); }
    finally { button.disabled = false; }
  }
  const downloadSessions = () => downloadList($('sessions-csv'), 'sessions', '/admin/api/sessions',
    ['No.', '세션 ID', '닉네임', '회원번호', '첫 질문', TERMS.turns, 'RBTI', FEEDBACK_LABELS.up, FEEDBACK_LABELS.down, statusLabel('failed'), statusLabel('interrupted'), `생성 (${state.tz.label})`, `최근 갱신 (${state.tz.label})`],
    (s, i, rows) => [rows.length - i, s.id, s.nickname, s.user_id, s.preview, s.turn_count, s.rbti, s.likes, s.dislikes, s.refused?.failed ?? 0, s.refused?.interrupted ?? 0, stamp(s.create_time), stamp(s.update_time)]);
  const downloadUsers = () => downloadList($('users-csv'), 'users', '/admin/api/users',
    ['닉네임', '회원번호', 'RBTI', `${TERMS.turns} (누적)`, `마지막 질의 (${state.tz.label})`, `가입(첫 인증) (${state.tz.label})`],
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
      if (preservePosition) for (const b of blocks) b.open = openTurns.has(b.dataset.turnId);
      else blocks[0].open = true;
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
    const td = el('td');
    td.append(value);
    if (typeof value === 'string') td.title = value;
    return td;
  }
  function table(columns, rows, onRow, sorting, lead) {
    const wrap = el('div', 'table-wrap');
    const grid = el('table');
    const head = el('thead');
    const titles = el('tr');
    const body = el('tbody');
    let current = { key: sorting?.key, dir: sorting?.dir };
    const headers = columns.map((column) => {
      const th = el('th');
      th.scope = 'col';
      if (!sorting || (sorting.onSort && !column.sort)) { th.textContent = column.label; return th; }
      const button = el('button', 'sort-button', column.label);
      button.type = 'button';
      button.onclick = () => {
        const dir = current.key === column.key && current.dir === 'asc' ? 'desc' : 'asc';
        if (sorting.onSort) { sorting.onSort(column.key, dir); return; }
        current = { key: column.key, dir };
        mark();
        fill(sortRows(rows, column.key, dir));
      };
      th.append(button);
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
      for (const column of columns) tr.append(cellOf(column, row[column.key]));
      if (onRow) {
        tr.dataset.record = String(index);
        tr.tabIndex = 0;
        tr.setAttribute('aria-label', `${index + 1}번째 기록 상세 열기`);
        tr.onclick = () => onRow(row);
        tr.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onRow(row); } };
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
  const percent = (value) => value == null ? '미측정' : `${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%`;
  const points = (value) => `${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%p`;
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
  function kpi(label, value, { current, previous, format, upIsBad, sub, open } = {}) {
    const box = el(open ? 'button' : 'div', 'kpi');
    if (open) { box.type = 'button'; box.onclick = open.go; box.title = open.title; }
    box.append(el('span', null, label), el('strong', null, value));
    if (format && current != null && previous != null && (previous !== 0 || format === points)) {
      const worse = upIsBad && current !== previous ? (current > previous ? ' bad' : ' good') : '';
      box.append(el('span', `delta${worse}`, `${change(current, previous, format)} · 이전 기간 대비`));
    }
    if (sub) box.append(el('span', 'kpi-sub', sub));
    return box;
  }

  /** 피드백 KPI — 좋아요·싫어요 각각을 누르면 이 기간 + 그 평가로 걸러 대화 탭을 연다.
   *  (대화 목록 기간은 세션 최근 갱신 기준이라 평가 시각 기준인 카드 수와 조금 다를 수 있다.) */
  function feedbackKpi(summary, prior, period) {
    const box = kpi('피드백 (좋아요 · 싫어요)', `${num(summary.likes)} · ${num(summary.dislikes)}`, { sub: prior?.likes || prior?.dislikes ? `이전 기간 좋아요 ${num(prior.likes)} · 싫어요 ${num(prior.dislikes)}` : '' });
    const links = el('span', 'kpi-links');
    for (const rating of Object.keys(FEEDBACK_LABELS)) {
      const link = el('button', 'link-button', `${FEEDBACK_LABELS[rating]} 대화 보기`);
      link.type = 'button';
      link.onclick = () => openSessions({ since: period.since, until: period.until, rating });
      links.append(link);
    }
    box.append(links);
    return box;
  }

  function loadDashboard() {
    const params = new URLSearchParams({ since: $('dashboard-since').value, until: $('dashboard-until').value });
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
      kpi(`${TERMS.users} (기간)`, metric(summary.users), { current: summary.users, previous: prior?.users, format: num, sub: '이 기간에 대화한 사용자', open: { title: '회원 목록 보기', go: () => switchTab('users') } }),
      kpi(TERMS.turns, metric(summary.turns), { current: summary.turns, previous: prior?.turns, format: num, open: { title: '이 기간의 대화 보기', go: () => openSessions({ since, until }) } }),
      kpi('답변거절률', percent(refusalRate(summary)), { current: refusalRate(summary), previous: refusalRate(prior), format: points, upIsBad: true, sub: `${refusedText(summary.refused)} / ${num(summary.turns)}질의`, open: { title: '이 기간의 거절(실패·중단) 대화 보기', go: () => openSessions({ since, until, status: 'refused' }) } }),
      feedbackKpi(summary, prior, data.period),
      kpi(TERMS.clicks, metric(summary.clicks), { current: summary.clicks, previous: prior?.clicks, format: num, open: { title: '통계의 클릭 보기(같은 기간)', go: () => openStatistics('click') } }),
    );
    // 하루(오늘)면 선이 점 하나라 차트 대신 요약 수치. 여러 날이면 질의 없는 날은 선이 끊긴다(0초가 아니라 미측정).
    const latencyChart = days.length === 1 ? analysisCard('응답 시간', latencySummary(summary)) : charts.lines({
      title: '응답 시간', categories: days, format: seconds,
      note: `기간 전체 p50 ${seconds(summary.p50_seconds)} · p95 ${seconds(summary.p95_seconds)} · 질의가 없는 날은 선이 끊깁니다.`,
      series: [['p50_seconds', 'p50', 'series-1'], ['p95_seconds', 'p95', 'series-2']]
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
    for (const [label, value] of [['p50', summary.p50_seconds], ['p95', summary.p95_seconds]]) { const item = el('span', null, `${label} `); item.append(el('b', null, seconds(value))); box.append(item); }
    return box;
  }

  /** 비용 패널(owner) — 비용 KPI 2개 · 일별 비용 · 사용자별 비용 상위. 모델명은 서버가 싣지 않는다. */
  function costPanel(cost, days) {
    if (cost instanceof Error) return [analysisCard('비용', el('p', 'error', `비용을 불러오지 못했습니다: ${cost.message}`))];
    const summary = cost.summary, prior = cost.comparison?.summary;
    const { krw_per_usd: krw, krw_as_of: krwAsOf } = cost.currency;
    const kpis = el('div', 'kpis');
    kpis.append(
      kpi('비용 (USD 추정)', usd(summary.cost_usd), { current: summary.cost_usd, previous: prior?.cost_usd, format: usd, sub: krw && summary.cost_usd != null ? `≈ ₩${num(Math.round(summary.cost_usd * krw))} · 환율 ${krwAsOf ?? '기준일 미기재'} 기준` : '' }),
      kpi('과금 턴당 비용', usd(summary.cost_per_turn_usd), { current: summary.cost_per_turn_usd, previous: prior?.cost_per_turn_usd, format: usd, sub: `/ ${num(summary.priced_rows)} 과금 턴` }),
    );
    const byDay = new Map(cost.daily.map((row) => [row.day, row.cost_usd]));
    // 그날 비용이 null(전부 단가 미등록)이면 null 그대로 — 0으로 바꾸면 표·툴팁이 '$0.00'을 말한다.
    const chart = days.length > 1 && charts.stackedBars({
      title: '일별 비용', categories: days, unit: 'usd', format: usd,
      series: [{ label: '비용', className: 'series-1', values: days.map((day) => byDay.has(day) ? byDay.get(day) : 0) }],
      notes: { summary: '추정치 · 자세히', lines: cost.notes.map((item) => item.text) },
    });
    const users = analysisCard('사용자별 비용 상위', cost.users.length ? table([
      {key: 'nickname', label: '닉네임', format: day}, {key: 'user_id', label: '회원번호'}, {key: 'priced_rows', label: '과금 턴', format: metric}, {key: 'cost_usd', label: '비용', format: usd}, {key: 'cost_per_turn_usd', label: '과금 턴당 비용', format: usd},
    ], cost.users.map((row) => ({ ...row, nickname: row.nickname && row.nickname !== row.user_id ? row.nickname : null }))) : el('p', 'analysis-note', '사용자에 귀속된 사용량 기록이 없습니다.'), '메인 에이전트 기록 기준입니다(서브콜은 사용자 귀속이 없음).');
    const head = el('div', 'section-head');
    head.append(el('h3', null, '비용'), el('span', null, `${roleLabel('owner')} 전용 · 추정치`));
    return [head, kpis, ...(chart ? [chart] : []), users];
  }

  /** 기록 표 — 라벨 · 값 두 열. columns가 있으면 그 순서·라벨·시각 서식, 없으면 키 이름 그대로. */
  function showRecord(record, columns = state.recordColumns) {
    state.recordColumns = columns;
    const rows = (columns || Object.keys(record || {}).map((key) => ({ key, label: key })))
      .map((column) => ({ label: column.label, value: column.format ? column.format(record[column.key]) : valueText(record[column.key]) }));
    const grid = record && table([{ key: 'label', label: '항목' }, { key: 'value', label: '값' }], rows);
    grid?.querySelector('thead').remove();  // 라벨 · 값 두 열이라 머리행('항목 · 값')은 정보가 없다
    $('record-view').replaceChildren(...(grid ? [grid] : []));
  }

  /** 옆 패널(비모달) — 회원·초기 질문·관리자 행과 관리 폼이 같은 패널을 쓴다. 폼은 #record-actions에 붙는다.
   *  비모달이라 패널을 연 채 다른 행을 누르면 내용만 바뀐다. */
  function openDialog(title, record, columns = null) {
    if ($('record-dialog').open && !canLeavePanel()) return false;
    $('record-title').textContent = title;
    showRecord(record, columns);
    $('record-actions').replaceChildren();
    $('record-dialog').show();
    return true;
  }
  /** 옆 패널을 떠나도 되는지 — 폼에 저장하지 않은 변경(form.dirty)이 있으면 묻는다(다른 행·Esc·×·화면 이동 공통). */
  const canLeavePanel = () => ![...$('record-actions').querySelectorAll('form')].some((form) => form.dirty?.()) || confirm('저장하지 않은 변경이 있습니다. 버리고 나갈까요?');
  const closePanel = () => { if ($('record-dialog').open && !canLeavePanel()) return false; $('record-dialog').close(); return true; };

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
  const SOURCE_KINDS = { auto: '자동', manual: '수동' };
  /** RBTI 배지 버튼 — 누르면 그 유형으로 거른다(행 클릭과 겹치지 않게 전파를 막는다). */
  function rbtiButton(name, code) {
    const button = el('button', 'rbti', code);
    button.type = 'button';
    button.title = `${code}만 보기 · 다시 누르면 해제`;
    button.onclick = (event) => { event.stopPropagation(); toggleRbti(name, code); };
    return button;
  }
  const USER_COLUMNS = [
    { key: 'nickname', label: '닉네임', format: day, sort: true }, { key: 'user_no', label: '회원번호', sort: true }, { key: 'rbti', label: 'RBTI', format: (code) => (code ? rbtiButton('users', code) : '-'), sort: true },
    { key: 'turns', label: `${TERMS.turns} (누적)`, format: num, sort: true },
    // users.created_at = 이 회원의 첫 API 키 인증 때 만든 행(auth.py _register) — '가입'이 아니라 첫 인증 시각.
    { key: 'last_chat_at', label: '마지막 질의', format: when, sort: true }, { key: 'created_at', label: '가입(첫 인증)', format: when, sort: true },
  ];
  // 옆 패널 — 회원번호에 복사 버튼.
  const USER_DETAIL = USER_COLUMNS.map((column) => (column.key !== 'user_no' ? column : { ...column, format: (value) => {
    const cell = el('span', null, value);
    cell.append(' ', copyButton(value));
    return cell;
  } }));
  // 슬롯은 내부 키 대신 칩 라벨(서버가 서빙과 같은 규칙으로 채움). 노출 기간은 편집 패널에만.
  const STARTER_COLUMNS = [
    { key: 'label', label: '슬롯', sort: true }, { key: 'text', label: '문장' }, { key: 'source', label: '출처 종류', format: (v) => SOURCE_KINDS[v] ?? v }, { key: 'run_date', label: '생성일', format: day, sort: true },
    { key: 'pinned', label: '고정', format: (v) => flag(v, '고정'), sort: true }, { key: 'active', label: TERMS.active, format: (v) => flag(v, '노출', '중지'), sort: true },
  ];
  // 패널엔 슬롯 내부 키를 싣지 않는다 — 운영자에겐 칩 라벨('슬롯')이 이름이다.
  const STARTER_DETAIL = [...STARTER_COLUMNS, { key: 'valid_from', label: '노출 시작일', format: day }, { key: 'valid_until', label: '노출 종료일', format: day }];
  // 편집 폼이 있으면(운영자 이상) 폼이 다루는 문장·고정·노출·기간은 표에서 빼고 읽기 전용만 남긴다.
  const STARTER_READONLY = STARTER_DETAIL.filter((column) => ['label', 'source', 'run_date'].includes(column.key));

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
  const loadUsers = () => loadList('users', `/admin/api/users?${new URLSearchParams(state.applied.users)}`, USER_COLUMNS, openUser, undefined, (data) => {
    addMissingOptions($('users-rbti'), data.rbti_types);
    // 대화 상세의 '회원 보기'로 왔으면 그 회원 패널을 연다(회원번호 정확 일치 검색의 결과 행).
    const wanted = state.pendingMember, hit = data.items.find((row) => String(row.user_no) === wanted);
    state.pendingMember = null;
    if (hit) openUser(hit);
  });
  const loadStarters = () => {
    manage.starterTools($('starters-tools'));
    return loadList('starters', '/admin/api/starters', STARTER_COLUMNS, openStarter, undefined, (data) => { state.starterSlots = data.slots; });
  };

  // ── 감사 로그(관리자 전용): 서버가 준 행(target_type·action·before/after JSON)을 사람이 읽는 줄로 옮긴다.
  // 라벨 표는 이 한 곳이다 — 모르는 값은 원문 그대로 보인다(새 작업이 생겨도 빈칸이 되지 않게).
  const AUDIT_TARGETS = { '': '전체', login: '로그인', admin: '계정', user: '회원', starter: '초기 질문', session: '대화' };
  const AUDIT_ACTIONS = {
    'login:ok': '로그인', 'login:failed': '로그인 실패', 'login:logout': '로그아웃',
    'admin:create': '계정 생성', 'admin:update': '계정 변경', 'admin:password_reset': '비밀번호 재설정', 'admin:password_change': '비밀번호 변경',
    'user:update': '회원 변경',
    'starter:create': '초기 질문 추가', 'starter:update': '초기 질문 수정', 'starter:deactivate': '초기 질문 비활성', 'starter:generate': '초기 질문 생성',
    'session:purge': '대화 영구 삭제',
  };
  const AUDIT_FIELDS = {
    username: '계정명', role: '역할', is_active: '활성', text: '문장', pinned: '고정', active: '노출', valid_from: '노출 시작일', valid_until: '노출 종료일',
    slot: '슬롯', status: '결과', inserted: '추가 수', force: '다시 생성', user_id: '회원번호', turns: '질의 수', deleted_at: '삭제 요청 시각', app_name: '앱',
  };
  const auditValue = (field, value) => (value == null ? '-' : field === 'role' ? roleLabel(value) : typeof value === 'boolean' ? (value ? '예' : '아니오') : typeof value === 'object' ? JSON.stringify(value) : String(value));
  /** 바뀐 필드 — [라벨, 이전, 이후]. 'by'(CLI 표시)는 계정 칸이 말하므로 뺀다. */
  const auditChanges = (row) => [...new Set([...Object.keys(row.before ?? {}), ...Object.keys(row.after ?? {})])].filter((field) => field !== 'by')
    .map((field) => [AUDIT_FIELDS[field] ?? field, row.before && field in row.before ? auditValue(field, row.before[field]) : null, row.after && field in row.after ? auditValue(field, row.after[field]) : null]);
  /** 대상 — 종류 앞말 + 사람이 읽는 이름. 로그인 기록은 실제 계정일 때만 이름이 온다(서버가 입력 원문을
   *  싣지 않는다 — 아이디 칸에 친 비밀번호가 보이지 않게). 세션은 id 앞 8자, 초기 질문은 #id. */
  function auditTarget(row) {
    const name = row.target_name;
    if (name == null) return row.target_type === 'login' ? '알 수 없는 계정' : `${AUDIT_TARGETS[row.target_type] ?? row.target_type} -`;
    const noun = row.target_type === 'login' ? AUDIT_TARGETS.admin : AUDIT_TARGETS[row.target_type] ?? row.target_type;
    return `${noun} ${row.target_type === 'session' ? String(name).slice(0, 8) : row.target_type === 'starter' && /^\d+$/.test(name) ? `#${name}` : name}`;
  }
  const auditRow = (row) => ({
    ...row,
    who: row.actor_name ?? (row.after?.by === 'cli' ? 'CLI' : row.target_type === 'login' ? '-' : '시스템'),
    what: AUDIT_ACTIONS[`${row.target_type}:${row.action}`] ?? `${row.target_type} ${row.action}`,
    target: auditTarget(row),
    change: auditChanges(row).map(([label, before, after]) => `${label}: ${[before, after].filter((v) => v != null).join(' → ')}`).join(' · ') || '-',
  });
  const AUDIT_COLUMNS = [
    { key: 'created_at', label: '시각', format: when }, { key: 'who', label: '계정' }, { key: 'what', label: '작업' },
    { key: 'target', label: '대상' }, { key: 'change', label: '변경 내용' }, { key: 'ip', label: 'IP', format: day },
  ];
  function openAudit(row) {
    const record = { 시각: stamp(row.created_at), 계정: row.who, 작업: row.what, 대상: row.target, IP: row.ip ?? '-' };
    for (const [label, before, after] of auditChanges(row)) record[label] = `${before ?? '-'} → ${after ?? '-'}`;
    openDialog(`감사 기록 · ${row.what}`, record);
  }
  const downloadAudit = () => downloadList($('audit-csv'), 'audit', '/admin/api/audit',
    [`시각 (${state.tz.label})`, ...AUDIT_COLUMNS.slice(1).map((column) => column.label)],
    // 화면의 빈 칸 표시('-')는 파일에선 빈 칸 — 선두 '-'는 셀 규칙이 '-로 감싼다.
    (raw) => { const row = auditRow(raw); return [stamp(row.created_at), ...AUDIT_COLUMNS.slice(1).map((column) => (row[column.key] == null || row[column.key] === '-' ? '' : row[column.key]))]; });
  /** 계정 선택지 = 기록에 남은 계정 이름(서버 actors, 스냅샷 이름). 처음 한 번 채운다. */
  function loadAudit() {
    return loadList('audit', `/admin/api/audit?${new URLSearchParams(state.applied.audit)}`, AUDIT_COLUMNS, openAudit, auditRow, (data) => {
      addMissingOptions($('audit-actor'), data.actors);
    });
  }

  function openUser(row) {
    if (!openDialog(`회원 ${row.nickname ?? row.user_no}`, row, USER_DETAIL)) return;
    // 대화 탭의 검색이 회원번호 일치를 받는다 — 그 회원의 대화만 걸고 첫 대화를 연다.
    const sessions = el('button', null, '이 회원의 대화 보기');
    sessions.onclick = () => openSessions({ q: row.user_no });
    $('record-actions').append(sessions);
  }

  function openStarter(row) {
    if (!openDialog('초기 질문', row, hasRole('editor') ? STARTER_READONLY : STARTER_DETAIL)) return;
    manage.starterPanel(row);
  }

  function readQuery(name) {
    const values = {};
    for (const [key, id] of Object.entries(queryForms[name].fields)) {
      if ($(id).closest('label')?.hidden || $(id).disabled) continue;
      if ($(id).value.trim()) values[key] = $(id).value.trim();
    }
    return values;
  }

  const queryLabels = {q: '검색', since: '기간', rating: '피드백', status: '상태', target: '작업', actor: '계정', rbti: 'RBTI', nickname: '닉네임'};
  const PRESENCE = { any: '있음', none: '없음' };
  /** 칩 값 글자 — 값 세그먼트(숨은 칸)는 그 버튼 글자가 정본이다(라벨 표를 따로 두지 않는다). */
  function queryValue(name, key, value) {
    const input = $(queryForms[name].fields[key]);
    if (input.type === 'hidden') return [...$(input.dataset.segment).children].find((button) => button.dataset.value === value)?.textContent ?? value;
    return PRESENCE[value] ?? value;
  }

  /** 적용 중인 조건을 칩(키: 값 ×)으로 — 칩은 그 조건만, '초기화'는 전부 풀고 다시 읽는다. 기본 조건(전체)이면 줄째 숨긴다. */
  function queryNote(name) {
    const applied = state.applied[name] || {};
    const dirty = JSON.stringify(readQuery(name)) !== JSON.stringify(applied);
    const note = $('query-note-' + name);
    // 기간(시작·종료)은 칩 하나 '기간: A ~ B' — 해제도 둘 함께(clearCondition).
    const { since, until, ...rest } = applied;
    const entries = [...(since || until ? [['since', `${since ?? ''} ~ ${until ?? ''}`.trim()]] : []), ...Object.entries(rest)];
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
    note.replaceChildren(...(dirty ? [el('span', 'dirty', '조건 변경됨 · 조회를 눌러 적용하세요.')] : []), ...chips, ...(chips.length ? [reset] : []));
    note.hidden = !dirty && !chips.length;
  }
  /** 조건 칸 하나 비우기 — 세그먼트로 고르는 값(data-segment)은 그 묶음의 '전체'로, 직접 기간 칸은 닫는다. */
  function clearField(input) {
    input.value = '';
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
    for (const id of Object.values(fields)) clearField($(id));
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
  /** 데이터에서 오는 선택지(RBTI 코드·감사 계정) — 아직 없는 값만 덧붙인다(해시가 먼저 넣은 값과 겹치지 않게). */
  function addMissingOptions(select, values) {
    const known = new Set([...select.options].map((option) => option.value));
    select.append(...values.filter((value) => !known.has(value)).map((value) => new Option(value, value)));
  }

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
    counted('sessions', `${TERMS.sessions} 수`, '세션수', '회', 'tone-blue'), counted('users', TERMS.users, '활성사용자수', '명', 'tone-green'), counted('queries', TERMS.turns, '질의수', '회', 'tone-grey'),
    { key: 'queries_per_session', label: `${TERMS.sessions}당 평균 ${TERMS.turns}`, excel: '세션당 평균 질의수', unit: '회', format: fixed2 }, { key: 'avg_session_seconds', label: `평균 ${TERMS.sessions} 시간`, excel: '평균 세션시간', format: minutes },
    { ...counted('refusals', '답변거절 수', '답변거절수', '회', 'tone-red'), detail: (row) => refusedText(row.refused) }, counted('links', '링크 제공 수', '링크제공수', '회', 'tone-blue'), counted('clicks', TERMS.clicks, '클릭수', '회', 'tone-yellow'),
    { key: 'click_rate', label: '클릭률', excel: '클릭률', format: (value) => value == null ? '-' : `${value.toFixed(2)}%` },
  ];
  const statColumn = Object.fromEntries(STAT_COLUMNS.map((column) => [column.key, column]));
  const LEAD = ['day', 'weekday'];
  // 묶음별 카드 행 · 추이 선 · 요일 막대 · 표 열. 표는 전체 묶음이 11열 전부이고, CSV는 묶음과 무관하게 11열이다.
  const STAT_VIEWS = {
    all: { cards: [['sessions', 'queries', 'links'], ['users', 'queries_per_session', 'clicks']], series: ['sessions', 'queries'], columns: STAT_COLUMNS.map((column) => column.key) },
    session: { cards: [['sessions', 'users', 'avg_session_seconds']], series: ['sessions', 'users'], columns: [...LEAD, 'sessions', 'users', 'avg_session_seconds'],
      weekday: { key: 'sessions', title: '요일별 평균 세션 수', insight: '세션이 가장 많이 발생된 요일' } },
    query: { cards: [['queries', 'queries_per_session', 'refusals']], series: ['queries', 'refusals'], columns: [...LEAD, 'queries', 'queries_per_session', 'refusals'],
      weekday: { key: 'queries', title: '요일별 평균 질의 수', insight: '질의가 가장 많은 요일' } },
    click: { note: '클릭은 클릭한 시각의 날짜로 세고 같은 링크를 다시 눌러도 셉니다 — 그래서 클릭률이 100%를 넘을 수 있습니다.',
      cards: [['links', 'clicks', 'click_rate']], series: ['links', 'clicks'], columns: [...LEAD, 'links', 'clicks', 'click_rate'],
      weekday: { key: 'clicks', title: '요일별 평균 클릭 수', insight: '클릭이 가장 많이 일어난 요일' } },
  };

  /** 기간 바(대시보드·통계 공용, name = 'dashboard' | 'statistics'). 프리셋은 presetRange(오늘 포함)로 날짜 칸을
   *  채우고, '직접'은 기간 표시 대신 날짜 칸을 연다(crema의 기간 버튼 → 달력 패널 자리). */
  function setPeriod(name, range) {
    for (const button of $(`${name}-filters`).querySelectorAll('[data-range]')) button.setAttribute('aria-pressed', String(button.dataset.range === range));
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
    state.statisticsView = view;
    for (const button of $('statistics-views').children) button.setAttribute('aria-pressed', String(button.dataset.view === view));
    state.periodFrom = 'dashboard';
    switchTab('statistics');
  }
  /** 기간 표시 문구(기간 바의 '기간 YYYY-MM-DD ~ YYYY-MM-DD'). */
  const showPeriod = (name) => { $(`${name}-range`).textContent = `${$(`${name}-since`).value} ~ ${$(`${name}-until`).value}`; };

  function loadStatistics() {
    const params = new URLSearchParams({ since: $('statistics-since').value, until: $('statistics-until').value });
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
    return cardBox(column.label, column.format(value), value == null ? '' : column.unit, range, column.detail?.(summary));
  }
  /** 카드 한 장 — 라벨 · 값(+단위) · 보조 문구(세부가 있으면 세부, 아니면 기간). */
  function cardBox(label, text, unit, range, detail) {
    const card = el('div', 'crema-card'), figure = el('div', 'crema-value');
    figure.append(el('strong', null, text));
    if (unit) figure.append(el('span', null, unit));
    card.append(el('span', 'crema-label', label), figure, detail ? el('span', 'crema-sub crema-detail', detail) : el('span', 'crema-sub', range));
    return card;
  }

  /** RBTI 묶음 — 기간 합계만(일별 표·엑셀 11열 고객 양식에는 넣지 않는다). 유형은 서버가 데이터에 나온 코드만,
   *  많은 순으로 준다. 적용률 = RBTI가 적용된 질의 ÷ 기간 질의수. */
  function rbtiBlocks(data, range) {
    const applied = data.rbti.reduce((sum, row) => sum + row.turns, 0), queries = data.summary.queries;
    const percentOf = (part, whole) => (whole ? `${(part / whole * 100).toFixed(1)}%` : '-');
    const cards = el('div', 'crema-cards'), line = el('div', 'crema-row');
    line.append(cardBox('RBTI 적용률', percentOf(applied, queries), '', range, `${num(applied)} / ${num(queries)}질의`), cardBox(`RBTI 적용 ${TERMS.turns}`, num(applied), '회', range));
    cards.append(line);
    const bars = charts.stackedBars({
      title: `RBTI 유형별 ${TERMS.turns}`, categories: data.rbti.map((row) => row.rbti), tick: String, axisTitle: 'RBTI 유형', unit: 'count', format: num,
      onPick: (code) => openPeriodSessions(data.period, { rbti: code }),
      series: [{ label: TERMS.turns, className: 'tone-blue', values: data.rbti.map((row) => row.turns) }],
    });
    bars.classList.add('crema-bars');
    // 비율 열은 따로 키를 둔다 — 같은 키면 머리 정렬 표시가 두 열에 함께 붙는다.
    const rows = data.rbti.map((row) => ({ ...row, share: applied ? row.turns / applied : null }));
    const grid = table([{ key: 'rbti', label: 'RBTI 유형' }, { key: 'turns', label: TERMS.turns, format: num }, { key: 'share', label: '적용 질의 중 비율', format: (share) => (share == null ? '-' : `${(share * 100).toFixed(1)}%`) }], rows, (row) => openPeriodSessions(data.period, { rbti: row.rbti }), {});
    const wrap = el('div', 'bleed');  // 회원·관리자 표와 같은 목록 표(일별 표의 날짜·요일 열 서식은 안 맞는다)
    wrap.append(grid);
    return [cards, bars, ...(data.rbti.length ? [wrap] : [])];
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
      title: '일별 피드백', categories: days, unit: 'count', format: num,
      series: [['likes', 'tone-blue'], ['dislikes', 'tone-grey']].map(([key, className]) => ({ label: key === 'likes' ? FEEDBACK_LABELS.up : FEEDBACK_LABELS.down, className, values: days.map((day) => byDay.get(day)?.[key] ?? 0) })),
    });
    bars.classList.add('crema-bars');
    const comments = data.feedback.comments, head = el('div', 'section-head');
    head.append(el('h3', null, '의견이 달린 피드백'), el('span', null, `최근 ${num(comments.length)}건 · 누르면 그 대화를 엽니다`));
    const list = comments.length ? table([
      { key: 'updated_at', label: '평가 시각', format: when }, { key: 'rating', label: '평가', format: (v) => el('em', `feedback-${v}`, FEEDBACK_LABELS[v] ?? v) },
      { key: 'question', label: '질문', format: day }, { key: 'comment', label: '의견' },
    ], comments, (row) => { switchTab('sessions'); selectSession({ id: row.session_id, user_id: row.user_id }); }) : el('div', 'placeholder', '이 기간에 의견이 달린 피드백이 없습니다.');
    const wrap = el('div', 'bleed');
    wrap.append(list);
    return [cards, bars, head, wrap];
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
    const trend = charts.lines({
      title: `${range} 기준`, categories: oldestFirst.map((row) => row.day), format: num,
      // 범례엔 기간 합계를 붙이고(legend), 툴팁·표는 계열명만(label) — 합계가 칸 값 옆에 붙으면 '12 세션수 2,322'로 읽힌다.
      series: view.series.map((key) => ({ label: statColumn[key].label, legend: `${statColumn[key].label} ${num(data.summary[key])}`, className: statColumn[key].tone, values: oldestFirst.map((row) => row[key]) })),
    });
    const blocks = [...(view.note ? [el('p', 'crema-note', view.note)] : []), cards, trend];
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
    const head = el('div', 'crema-table-head'), download = el('button', 'crema-download');
    download.type = 'button';
    download.append($('download-icon').content.firstElementChild.cloneNode(true), document.createTextNode('엑셀 다운로드'));
    download.onclick = downloadStatistics;
    head.append(el('span', null, `${num(data.daily.length)}일 · 행을 누르면 그날 대화`), download);
    // 맨 위 고정 행 = 기간 전체(합계가 아니라 기간으로 다시 센 요약 — 활성 사용자는 기간 고유 수). 정렬해도 머리에 남는다.
    const grid = table(view.columns.map((key) => statColumn[key]), data.daily, (row) => openPeriodSessions({ since: row.day, until: row.day }), {},
      { ...data.summary, day: '기간 전체', weekday: '' });
    grid.classList.add('crema-table', 'bleed');
    $('statistics').replaceChildren(...blocks, head, grid);
  }

  /** 엑셀 다운로드 = 일별 11열(최신순, 화면과 같은 서식)을 UTF-8 BOM CSV로. */
  function downloadStatistics() {
    const data = state.statistics;
    if (!data) return;
    // 값이 없는 칸(분모 0)은 빈 칸이다 — 화면의 '-'는 선두 '-'라 셀 규칙이 '로 감싸 엑셀에 '-로 보인다.
    saveCsv(`statistics_${data.period.since}_${data.period.until}.csv`, [
      STAT_COLUMNS.map((column) => column.excel),
      ...data.daily.map((row) => STAT_COLUMNS.map((column) => (row[column.key] == null ? '' : (column.csv || column.format)(row[column.key])))),
    ]);
  }


  const manage = initManage({ api, el, table, valueText, state, openDialog, showRecord, reload: () => loaders[state.tab]() });

  // 기간 바: 프리셋은 누르면 바로 조회, '직접'은 날짜 칸의 조회 버튼으로. 고른 쪽을 기억해 다른 화면이 이어 쓴다.
  for (const [name, load] of [['dashboard', loadDashboard], ['statistics', loadStatistics]]) {
    for (const button of $(`${name}-filters`).querySelectorAll('[data-range]')) button.onclick = () => {
      setPeriod(name, button.dataset.range);
      state.periodFrom = name;
      if (button.dataset.range !== 'custom') { $('app-error').hidden = true; load(); }
    };
    $(`${name}-filters`).onsubmit = (event) => { event.preventDefault(); state.periodFrom = name; $('app-error').hidden = true; load(); };
  }
  for (const button of $('statistics-views').querySelectorAll('[data-view]')) button.onclick = () => {
    state.statisticsView = button.dataset.view;
    for (const other of $('statistics-views').children) other.setAttribute('aria-pressed', String(other === button));
    writeHash();
    renderStatistics();
  };
  $('record-close').onclick = closePanel;
  $('record-dialog').onclose = () => { $('record-view').replaceChildren(); $('record-actions').replaceChildren(); };
  // 비모달 패널은 Esc를 스스로 받지 않는다 — 문서에서 받아 닫는다.
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closePanel(); });
  $('users-filters').onsubmit = (event) => { event.preventDefault(); applyQuery('users'); $('app-error').hidden = true; loadUsers(); };
  // 검색칸(type=search): Enter로 검색, 지우기(✕)로 비우면 곧바로 전체를 다시 읽는다. 대화 폼은 날짜 칸이 있어
  // 브라우저의 Enter 암묵 제출이 막히므로 Enter도 여기서 받는다.
  for (const [input, form] of [['q', 'filters'], ['users-q', 'users-filters']]) {
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
    const days = button.dataset.days;
    $(since).closest('.crema-custom').hidden = days !== 'custom';
    if (days === 'custom') return;
    [$(since).value, $(until).value] = days ? presetRange(days) : ['', ''];
    $(since).form.requestSubmit();
  };
  // 목록 필터 선택 상자 — 바꾸면 바로 다시 읽는다.
  for (const [select, form] of [['users-rbti', 'users-filters'], ['users-nick', 'users-filters'], ['sessions-rbti', 'filters']]) $(select).onchange = () => $(form).requestSubmit();

  function switchTab(tab) {
    if (!closePanel()) return;  // 패널은 연 화면의 행을 보여 준다 — 화면을 떠나면 닫는다(저장 안 한 변경은 묻는다)
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
    const tab = state.tab, params = new URLSearchParams(queryForms[tab] ? state.applied[tab] : {});
    if (state[PAGE_KEYS[tab]]) params.set('page', state[PAGE_KEYS[tab]]);
    if (state.sort[tab]) { params.set('sort', state.sort[tab].key); params.set('dir', state.sort[tab].dir); }
    if (tab === 'statistics' && state.statisticsView !== 'all') params.set('view', state.statisticsView);
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
        const input = $(id), value = params.get(key) ?? '';
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
    if (tab === 'statistics') {
      const view = params.get('view');
      state.statisticsView = [...$('statistics-views').children].some((button) => button.dataset.view === view) ? view : 'all';
      for (const button of $('statistics-views').children) button.setAttribute('aria-pressed', String(button.dataset.view === state.statisticsView));
    }
    if (tab === 'sessions') {
      state.currentSession = params.get('s') ? { id: params.get('s'), user_id: params.get('u') ?? '' } : null;
      $('sessions-sort').value = state.sort.sessions ? `${state.sort.sessions.key}:${state.sort.sessions.dir}` : '';
    }
  }
  /** 해시가 가리키는 화면 열기 — 세션이 지정돼 있으면 목록과 함께 그 상세도 연다. */
  function openFromHash() {
    // 저장 안 한 변경을 두고 떠나지 않기로 하면 상태를 바꾸기 전에 멈추고 주소를 지금 화면으로 되돌린다.
    if (!closePanel()) { writeHash(); return; }
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
  window.addEventListener('hashchange', () => { if (state.role) openFromHash(); });

  $('gate').onsubmit = async (e) => {
    e.preventDefault();
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
    catch (error) { showError(`로그아웃 요청에 실패했습니다: ${error.message}`); $('logout').disabled = false; }
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
    // 시간대가 정해진 뒤에야 날짜 칸 기본값(오늘 기준 프리셋)과 화면 라벨을 채울 수 있다.
    state.tz = me.timezone;
    for (const node of document.querySelectorAll('.tz')) node.textContent = state.tz.label;
    setPeriod('dashboard', '7');
    setPeriod('statistics', '7');
    $('account').replaceChildren(document.createTextNode(me.username), el('small', null, roleLabel(me.role)));
    $('toolbar-owner').hidden = me.role !== 'owner';
    $('gate').hidden = true;
    for (const id of ['sidebar', 'mobile-bar', 'main']) $(id).hidden = false;
    await Promise.all([loadOverview(), openFromHash()]);
  }
  start();
