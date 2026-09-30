import { initManage } from "/static/lib/admin_manage.js?v=4";
import { initCharts } from "/static/lib/admin_charts.js?v=6";
import { renderBody } from "/static/lib/md.js";
import { coverUrl, formatPrice, isSafeUrl, makeCoverImg, sourceCardType, sourceDomain, sourceTitle, CARD_LABELS } from "/static/lib/sources.js";

  const $ = (id) => document.getElementById(id);
  const state = { page: 0, tz: null, tab: 'dashboard', statistics: null, statisticsView: 'all', usersPage: 0, startersPage: 0, applied: {}, currentSession: null, listScroll: 0, username: null, role: null, roles: [] };
  const queryForms = {
    sessions: {form: 'filters', page: 'page', fields: {q: 'q', since: 'since', until: 'until'}},
    users: {form: 'users-filters', page: 'usersPage', fields: {q: 'users-q'}},
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
  /** epoch 초 → 어드민 시간대 시각 문자열(라벨 포함). */
  const clock = (ts) => (ts ? `${shifted(ts * 1000).toLocaleString('ko-KR', { timeZone: 'UTC' })} ${state.tz.label}` : '');

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
          target.replaceChildren(...(bar ? [bar] : []), el('div', 'placeholder', '불러오지 못했습니다. 새로고침으로 다시 시도하세요.'));
        }
      }
    } finally {
      if (pending.get(key) === controller) pending.delete(key);
    }
  }

  function loadOverview() {
    return request('stats', '/admin/api/overview', (o) => {
      // 용어는 통계 탭과 같다 — 활성 사용자 = 실제로 대화한 사용자, 세션 = 대화방(질문 여러 개가 한 세션).
      const stats = [['활성 사용자(누적)', num(o.chat_users), '실제로 대화한 사용자 수(전체 기간)'], ['세션', num(o.sessions), '대화방 수(질문 여러 개가 한 세션)'], ['질의', num(o.turns)]];
      $('stats').replaceChildren(...stats.map(([label, value, hint]) => {
        const box = el('div', 'stat');
        box.append(el('b', null, value), el('span', null, label));
        box.title = hint || `${label} ${value}`;
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

  /** crema 페이지 버튼(QueryAnalysis.tsx): ‹ · 10개 묶음 · › · 다음 묶음. page는 0부터. */
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
    target.replaceChildren(button('‹', page - 1, '이전 페이지'), ...numbers, button('›', page + 1, '다음 페이지'), button('»', last, '다음 10페이지'));
  }

  /** 사용자 칸 — 닉네임(서버가 모르면 회원번호로 채워 준다) + 다르면 작게 회원번호. 둘 다 textContent. */
  function userCell(s) {
    const cell = el('span', 'q-user', s.nickname);
    if (s.nickname !== s.user_id) cell.append(el('small', null, s.user_id));
    return cell;
  }

  /** 어드민 시간대 시각을 날짜·시각 두 줄로(목록의 질의일시). */
  const clockParts = (ts) => { const text = shifted(ts * 1000).toISOString(); return [text.slice(0, 10), `${text.slice(11, 19)} ${state.tz.label}`]; };

  function loadSessions() {
    const page = state.page;
    const params = new URLSearchParams({ ...state.applied.sessions, page });
    $('sessions').replaceChildren(el('div', 'placeholder', '대화를 불러오는 중…'));
    return request('sessions', `/admin/api/sessions?${params}`, (data) => {
      $('page-info').textContent = `${num(data.total)}건`;
      const rows = data.items.map((s) => {
        const row = el('button', 'query-row');
        row.type = 'button';
        row.dataset.id = s.id;
        row.setAttribute('aria-current', String(s.id === state.currentSession?.id));
        row.onclick = () => selectSession(s);
        // 카드 3줄(crema QueryAnalysis): ① 세션 id·거절 배지 … 날짜 시각 ② 첫 질문 ③ 사용자 · 외 N건.
        const top = el('span', 'q-top'), [day, time] = clockParts(s.update_time), when = el('span', 'q-time', day);
        when.append(el('small', null, time));
        top.append(el('span', 'q-id', s.id.slice(0, 8)));
        for (const [status, n] of Object.entries(s.refused ?? {})) if (n) top.append(el('em', `status-${status}`, `${statusLabel(status)} ${num(n)}`));
        top.append(when);
        const meta = el('small', null, s.nickname);
        if (s.turn_count > 1) meta.append(' · 외 ', el('em', null, num(s.turn_count - 1)), '건의 대화가 진행됨');
        row.append(top, el('b', s.preview ? null : 'empty', s.preview || '(사용자 발화 없음)'), meta);
        row.title = s.id;
        return row;
      });
      $('sessions').replaceChildren(...(rows.length ? rows : [el('div', 'placeholder', '조건에 맞는 대화가 없습니다. 검색 조건을 초기화해 보세요.')]));
      cremaPager($('sessions-pager'), data.total, data.page_size, page, (to) => { state.page = to; loadSessions(); $('sessions-list').scrollTop = $('sessions-pane').scrollTop = 0; });
      // 상세 칸을 비워 두지 않는다 — 아직 고른 대화가 없으면 첫 대화를 연다(좁은 화면은 목록에 머문다).
      if (!state.currentSession && data.items.length) selectSession(data.items[0], { reveal: false });
    });
  }

  /** 엑셀 다운로드 = 적용한 검색 조건의 목록 전체(페이지를 이어 받아 UTF-8 BOM CSV로). */
  async function downloadSessions() {
    const button = $('sessions-csv'), rows = [];
    button.disabled = true;
    try {
      for (let page = 0; ; page++) {
        const data = await api(`/admin/api/sessions?${new URLSearchParams({ ...state.applied.sessions, page })}`);
        rows.push(...data.items);
        if (rows.length >= data.total || !data.items.length) break;
      }
      saveCsv(`sessions_${localDay()}.csv`, [
        ['No.', '세션 ID', '닉네임', '회원번호', '첫 질문', '턴 수', `생성 (${state.tz.label})`, `최근 갱신 (${state.tz.label})`],
        ...rows.map((s, i) => [rows.length - i, s.id, s.nickname, s.user_id, s.preview, s.turn_count, clockParts(s.create_time).join(' '), clockParts(s.update_time).join(' ')]),
      ]);
    } catch (error) { if (error.name !== 'AbortError') showError(`다운로드 실패: ${error.message}`); }
    finally { button.disabled = false; }
  }

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
    if (src.rating) facts.append(el('span', 'rating', `★ ${src.rating}`), document.createTextNode(' '));
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
    if (t.status !== 'completed') meta.append(el('span', `status-${t.status}`, statusLabel(t.status)), ' · ');
    meta.append([clock(t.asked_at), t.elapsed_ms == null ? '' : `응답 ${(t.elapsed_ms / 1000).toFixed(1)}초`,
      t.likes || t.dislikes ? `좋아요 ${num(t.likes)} · 싫어요 ${num(t.dislikes)}` : '', t.clicks ? `클릭 ${num(t.clicks)}` : '',
      t.rbti_applied ? `RBTI ${t.rbti_applied}` : ''].filter(Boolean).join(' · '));
    const head = el('summary', 'turn-head fold');
    head.append(el('em', null, `#${i + 1}`), el('b', null, t.user_message), meta);
    const tools = el('div', 'turn-tools');
    tools.append(copyButton(t.user_message, '질문 복사'), copyButton(t.assistant_message, '답변 복사'));
    const answer = el('div', 'answer');
    const body = el('div', 'turn-body');
    body.append(tools, answer);
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
  }

  /** 상세 보기 — 첫 진입은 #1만 펼친다. preservePosition(새로고침)은 펼침·스크롤을 지킨다. */
  function selectSession(session, { preservePosition = false, reveal = !preservePosition } = {}) {
    const scrollTop = $('detail').scrollTop, infoOpen = !!$('detail').querySelector('details.info')?.open;
    const openTurns = new Set([...$('detail').querySelectorAll('details.turn[open]')].map((b) => b.dataset.turnId));
    state.currentSession = { ...session };
    for (const row of $('sessions').querySelectorAll('.query-row')) row.setAttribute('aria-current', String(row.dataset.id === session.id));
    if (reveal) showDetail(true);  // 새로고침·자동 선택은 좁은 화면의 목록/상세 칸을 그대로 둔다
    const bar = el('div', 'detail-bar bleed'), back = el('button', 'detail-back', '← 목록');
    const meta = el('span', 'detail-meta', [session.id.slice(0, 8), session.nickname].filter(Boolean).join(' · '));
    bar.id = 'detail-bar';  // request()가 오류 화면에서도 이 막대는 남긴다
    back.type = 'button';
    back.onclick = () => showDetail(false);
    bar.append(back, el('h2', null, '대화 상세'), meta);
    $('detail').replaceChildren(bar, el('div', 'placeholder', '대화를 불러오는 중…'));
    const params = new URLSearchParams({ user_id: session.user_id });
    return request('detail', `/admin/api/sessions/${encodeURIComponent(session.id)}?${params}`, (d) => {
      const m = d.metrics, turns = d.turns;
      const info = el('dl', 'info-grid bleed');
      const rows = [
        ['세션 ID', d.session.id, copyButton(d.session.id)], ['사용자', userCell(d.session), copyButton(d.session.user_id)],
        ['시작 일시', turns.length ? clock(turns[0].asked_at) : clock(d.session.create_time)], ['질의 횟수', `${num(m.turns)}회`],
        ['평균 응답속도', m.avg_turn_seconds == null ? '-' : `${m.avg_turn_seconds}초`], ['지속시간', minutes(m.duration_seconds)],
        // 지속시간과 같은 기준(chat_turn)의 마지막 응답 — sessions.update_time은 ADK 이벤트 기록 때 밀리는 값이라
        // 답변 완료(스트림 마감 뒤 기록)와 수 초 어긋난다(목록의 '최근 갱신'이 그 값이다).
        ['마지막 응답', turns.length ? clock(Math.max(...turns.map((t) => t.completed_at))) : '-'],
      ];
      for (const [label, value, tool] of rows) { const dd = el('dd'); dd.append(value, ...(tool ? [tool] : [])); info.append(el('dt', null, label), dd); }
      const [day, time] = clockParts(turns.length ? turns[0].asked_at : d.session.create_time);
      const who = d.session.nickname === d.session.user_id ? d.session.user_id : `${d.session.nickname}/${d.session.user_id}`;
      meta.textContent = `${d.session.id.slice(0, 8)} · ${d.session.nickname}`;
      const summary = el('summary', 'section-bar bleed fold'), label = el('span', null, '기본정보 ');
      label.append(el('small', null, [who, `${day.slice(5)} ${time.slice(0, 5)} 시작`, `${num(m.turns)}회`, m.avg_turn_seconds == null ? '' : `평균 ${m.avg_turn_seconds}초`].filter(Boolean).join(' · ')));
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
  const seconds = (value) => value == null ? '미측정' : `${Number(value).toLocaleString('ko-KR', { maximumFractionDigits: 2 })}초`;
  const metric = (value) => value == null ? '미측정' : num(value);

  function table(columns, rows, onRow) {
    const wrap = el('div', 'table-wrap');
    const grid = el('table');
    const head = el('thead');
    const titles = el('tr');
    for (const column of columns) { const th = el('th', null, column.label); th.scope = 'col'; titles.append(th); }
    head.append(titles);
    const body = el('tbody');
    rows.forEach((row, index) => {
      const tr = el('tr');
      for (const column of columns) {
        const value = column.format ? column.format(row[column.key]) : valueText(row[column.key]);
        const td = el('td');
        td.append(value);
        if (typeof value === 'string') td.title = value;
        tr.append(td);
      }
      if (onRow) {
        tr.dataset.record = String(index);
        tr.tabIndex = 0;
        tr.setAttribute('aria-label', `${index + 1}번째 기록 상세 열기`);
        tr.onclick = () => onRow(row);
        tr.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onRow(row); } };
      }
      body.append(tr);
    });
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

  const charts = initCharts({ template: $('chart-template'), el, table, timezone: () => state.tz.label });
  const usd = (value) => value == null ? '—' : `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: value && Math.abs(value) < 1 ? 4 : 2 })}`;
  const percent = (value) => value == null ? '미측정' : `${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%`;
  const points = (value) => `${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%p`;
  // 답변거절률 = (실패 + 중단) ÷ 질의 — 통계 탭 '답변거절수'와 같은 정의(서버 refusals).
  const refusalRate = (s) => s?.turns ? s.refusals / s.turns * 100 : null;
  const isOwner = () => state.roles.indexOf(state.role) >= state.roles.indexOf('owner');

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
    return difference && absolute === format(0) ? rate : `${sign}${absolute} (${rate})`;
  }

  /** KPI 타일. 증감 줄은 비교할 이전 값이 있을 때만 — 없거나(미측정) 0이면 변화율이 뜻이 없어 줄째 뺀다
   *  (비율 지표 %p는 이전 0%도 비교값이다). upIsBad면 증가를 빨강·감소를 초록으로 — 부호 글자가 함께 간다. */
  function kpi(label, value, { current, previous, format, upIsBad, sub } = {}) {
    const box = el('div', 'kpi');
    box.append(el('span', null, label), el('strong', null, value));
    if (format && current != null && previous != null && (previous !== 0 || format === points)) {
      const worse = upIsBad && current !== previous ? (current > previous ? ' bad' : ' good') : '';
      box.append(el('span', `delta${worse}`, `${change(current, previous, format)} · 이전 기간 대비`));
    }
    if (sub) box.append(el('span', 'kpi-sub', sub));
    return box;
  }

  function loadDashboard() {
    const params = new URLSearchParams({ since: $('dashboard-since').value, until: $('dashboard-until').value });
    showPeriod('dashboard');
    // 재조회는 이전 렌더를 흐리게 유지한다 — 자리표시자로 갈아 끼우면 화면이 튄다.
    if ($('dashboard').childElementCount) $('dashboard').classList.add('is-loading');
    else $('dashboard').replaceChildren(el('div', 'placeholder', '운영 지표를 불러오는 중…'));
    // 비용은 owner 전용 엔드포인트다 — 다른 역할은 부르지도 않는다(서버도 403).
    const cost = isOwner() ? api(`/admin/api/analytics/cost?${params}`).catch((error) => error) : Promise.resolve(null);
    return request('dashboard', `/admin/api/analytics?${params}`, async (data) => renderDashboard(data, await cost))
      .finally(() => { if (!pending.has('dashboard')) $('dashboard').classList.remove('is-loading'); });
  }

  /** 대시보드(모든 역할): 기간 KPI 5개(이전 기간 대비) · 응답 시간 추이 · 시간대별 질의. owner면 비용 패널을 덧붙인다. */
  function renderDashboard(data, cost) {
    const summary = data.summary, prior = data.comparison?.summary;
    const days = periodDays(data.period);
    const byDay = new Map(data.daily.map((row) => [row.day, row]));
    const daily = (pick, missing) => days.map((day) => byDay.has(day) ? pick(byDay.get(day)) : missing);

    const kpis = el('div', 'kpis');
    kpis.append(
      kpi('활성 사용자(기간)', metric(summary.users), { current: summary.users, previous: prior?.users, format: num, sub: '이 기간에 대화한 사용자' }),
      kpi('질의 수', metric(summary.turns), { current: summary.turns, previous: prior?.turns, format: num }),
      kpi('답변거절률', percent(refusalRate(summary)), { current: refusalRate(summary), previous: refusalRate(prior), format: points, upIsBad: true, sub: `${refusedText(summary.refused)} / ${num(summary.turns)}질의` }),
      kpi('피드백 (좋아요 · 싫어요)', `${num(summary.likes)} · ${num(summary.dislikes)}`, { sub: prior?.likes || prior?.dislikes ? `이전 기간 좋아요 ${num(prior.likes)} · 싫어요 ${num(prior.dislikes)}` : '' }),
      kpi('출처 클릭', metric(summary.clicks), { current: summary.clicks, previous: prior?.clicks, format: num }),
    );
    const latencyChart = charts.lines({
      title: '응답 시간', categories: days, format: seconds,
      note: `기간 전체 p50 ${seconds(summary.p50_seconds)} · p95 ${seconds(summary.p95_seconds)}.`,
      series: [['p50_seconds', 'p50', 'series-1'], ['p95_seconds', 'p95', 'series-2']]
        .map(([key, label, className]) => ({ label, className, values: daily((row) => row[key], null) })),
    });
    // 시간대별 질의(기간 합계) — 요일 분포는 통계 탭이 맡는다. 기록 없는 시각은 0.
    const hours = [...Array(24).keys()], byHour = new Map(data.hourly.map((row) => [row.hour, row.turns]));
    const hourly = charts.stackedBars({
      title: '시간대별 질의', categories: hours, unit: 'count', format: num,
      zone: '', tick: (hour) => `${hour}시`, axisTitle: `시각 (${state.tz.label})`,
      series: [{ label: '질의', className: 'series-1', values: hours.map((hour) => byHour.get(hour) ?? 0) }],
    });
    $('dashboard').replaceChildren(kpis, latencyChart, hourly, ...(cost ? costPanel(cost, days) : []));
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
    const chart = charts.stackedBars({
      title: '일별 비용', categories: days, unit: 'usd', format: usd,
      series: [{ label: '비용', className: 'series-1', values: days.map((day) => byDay.has(day) ? byDay.get(day) : 0) }],
      notes: { summary: '추정치 · 자세히', lines: cost.notes.map((item) => item.text) },
    });
    const users = analysisCard('사용자별 비용 상위', cost.users.length ? table([
      {key: 'nickname', label: '사용자'}, {key: 'user_id', label: '회원번호'}, {key: 'priced_rows', label: '과금 턴', format: metric}, {key: 'cost_usd', label: '비용', format: usd}, {key: 'cost_per_turn_usd', label: '과금 턴당 비용', format: usd},
    ], cost.users) : el('p', 'analysis-note', '사용자에 귀속된 사용량 기록이 없습니다.'), '메인 에이전트 기록 기준입니다(서브콜은 사용자 귀속이 없음).');
    const head = el('div', 'section-head');
    head.append(el('h3', null, '비용'), el('span', null, 'owner 전용 · 추정치'));
    return [head, kpis, chart, users];
  }

  /** 기록 표 — 라벨 · 값 두 열. columns가 있으면 그 순서·라벨·시각 서식, 없으면 키 이름 그대로. */
  function showRecord(record, columns = state.recordColumns) {
    state.recordColumns = columns;
    const rows = (columns || Object.keys(record || {}).map((key) => ({ key, label: key })))
      .map((column) => ({ label: column.label, value: column.format ? column.format(record[column.key]) : valueText(record[column.key]) }));
    $('record-view').replaceChildren(...(record ? [table([{ key: 'label', label: '항목' }, { key: 'value', label: '값' }], rows)] : []));
  }

  /** 옆 패널(비모달) — 회원·초기 질문·관리자 행과 관리 폼이 같은 패널을 쓴다. 폼은 #record-actions에 붙는다.
   *  비모달이라 패널을 연 채 다른 행을 누르면 내용만 바뀐다. */
  function openDialog(title, record, columns = null) {
    $('record-title').textContent = title;
    showRecord(record, columns);
    $('record-actions').replaceChildren();
    $('record-dialog').show();
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
  const SOURCE_KINDS = { auto: '자동', manual: '수동' };
  const USER_COLUMNS = [
    { key: 'nickname', label: '닉네임', format: day }, { key: 'user_no', label: '회원번호' },
    { key: 'is_active', label: '이용 가능', format: (v) => flag(v, '가능', '차단') }, { key: 'turns', label: '질의 수(누적)', format: num },
    { key: 'last_chat_at', label: '마지막 질의', format: when }, { key: 'created_at', label: '등록일', format: when },
  ];
  // 슬롯은 내부 키 대신 칩 라벨(서버가 서빙과 같은 규칙으로 채움). 노출 기간은 편집 패널에만.
  const STARTER_COLUMNS = [
    { key: 'label', label: '슬롯' }, { key: 'text', label: '문장' }, { key: 'source', label: '출처 종류', format: (v) => SOURCE_KINDS[v] ?? v }, { key: 'run_date', label: '생성일', format: day },
    { key: 'pinned', label: '고정', format: (v) => flag(v, '고정', '') }, { key: 'active', label: '활성', format: (v) => flag(v, '노출', '중지') },
  ];
  const STARTER_DETAIL = [...STARTER_COLUMNS, { key: 'slot', label: '슬롯 키' }, { key: 'valid_from', label: '노출 시작일', format: day }, { key: 'valid_until', label: '노출 종료일', format: day }];

  /** 목록 한 화면 — 표 + 건수 + crema 페이지 버튼. key는 pane 접두(users·starters), 페이지는 state[key + 'Page']. */
  function loadList(key, path, columns, onRow) {
    const page = state[key + 'Page'];
    $(key).replaceChildren(el('div', 'placeholder', '불러오는 중…'));
    return request(key, `${path}${path.includes('?') ? '&' : '?'}page=${page}`, (data) => {
      $(key + '-info').textContent = `${num(data.total)}건`;
      $(key).replaceChildren(data.items.length ? table(columns, data.items, onRow) : el('div', 'placeholder', '조건에 맞는 기록이 없습니다.'));
      cremaPager($(key + '-pager'), data.total, data.page_size, page, (to) => { state[key + 'Page'] = to; loaders[key](); });
    });
  }
  const loadUsers = () => loadList('users', `/admin/api/users?${new URLSearchParams(state.applied.users)}`, USER_COLUMNS, openUser);
  const loadStarters = () => { manage.starterTools($('starters-tools')); return loadList('starters', '/admin/api/starters', STARTER_COLUMNS, openStarter); };

  function openUser(row) {
    openDialog(`회원 ${row.nickname ?? row.user_no}`, row, USER_COLUMNS);
    // 대화 탭의 검색이 회원번호 일치를 받는다 — 그 회원의 대화만 걸고 첫 대화를 연다.
    const sessions = el('button', null, '이 회원의 대화 보기');
    sessions.onclick = () => {
      clearSessionFilters();
      $('q').value = row.user_no;
      applyQuery('sessions');
      state.currentSession = null;
      switchTab('sessions');
    };
    $('record-actions').append(sessions);
    manage.userPanel(row);
  }

  function openStarter(row) {
    openDialog('초기 질문', row, STARTER_DETAIL);
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

  const queryLabels = {q: '검색', since: '시작일', until: '종료일'};

  function queryNote(name) {
    const applied = state.applied[name] || {};
    const dirty = JSON.stringify(readQuery(name)) !== JSON.stringify(applied);
    const note = $('query-note-' + name);
    const summary = Object.entries(applied).map(([key, value]) => `${queryLabels[key] || key}: ${value}`).join(' · ');
    note.textContent = `${dirty ? '조건 변경됨 · 검색을 눌러 적용하세요. ' : ''}조회 조건: ${summary || '전체'}`;
    note.hidden = !dirty && !summary;  // 기본 조건(전체)은 알릴 것이 없다 — 조건이 걸렸거나 바뀌었을 때만 보인다
    note.classList.toggle('dirty', dirty);
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
  const dotted = (day) => day.replaceAll('-', '. ');
  // 건수는 화면에선 천 단위 구분, CSV에선 원값(엑셀이 수로 읽게). unit은 카드의 단위 글자, tone은 차트 색 클래스.
  const counted = (key, label, unit, tone) => ({ key, label, unit, tone, format: num, csv: String });
  const STAT_COLUMNS = [
    { key: 'day', label: '날짜', format: (day) => day.replaceAll('-', '.') }, { key: 'weekday', label: '요일', format: String },
    counted('sessions', '세션수', '회', 'tone-blue'), counted('users', '활성사용자수', '명', 'tone-green'), counted('queries', '질의수', '회', 'tone-grey'),
    { key: 'queries_per_session', label: '세션당 평균 질의수', unit: '회', format: fixed2 }, { key: 'avg_session_seconds', label: '평균 세션시간', format: minutes },
    { ...counted('refusals', '답변거절수', '회', 'tone-red'), detail: (row) => refusedText(row.refused) }, counted('links', '링크제공수', '회', 'tone-blue'), counted('clicks', '클릭수', '회', 'tone-yellow'),
    { key: 'click_rate', label: '클릭률', format: (value) => value == null ? '-' : `${value.toFixed(2)}%` },
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
  /** 기간 표시 문구(기간 바의 '기간 YYYY. MM. DD ~ …'). */
  const showPeriod = (name) => { $(`${name}-range`).textContent = `${dotted($(`${name}-since`).value)} ~ ${dotted($(`${name}-until`).value)}`; };

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
    const column = statColumn[key], value = summary[key], detail = column.detail?.(summary);
    const card = el('div', 'crema-card'), figure = el('div', 'crema-value');
    figure.append(el('strong', null, column.format(value)));
    if (column.unit && value != null) figure.append(el('span', null, column.unit));
    card.append(el('span', 'crema-label', column.label), figure, detail ? el('span', 'crema-sub crema-detail', detail) : el('span', 'crema-sub', range));
    return card;
  }

  function renderStatistics() {
    const data = state.statistics, view = STAT_VIEWS[state.statisticsView];
    if (!data) return;
    const range = `${dotted(data.period.since)} ~ ${dotted(data.period.until)}`;
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
        title, categories: data.weekday.map((row) => row.weekday), zone: '', tick: String, axisTitle: '요일', format: average,
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
    head.append(el('span', null, `${num(data.daily.length)}건`), download);
    const grid = table(view.columns.map((key) => statColumn[key]), data.daily);
    grid.classList.add('crema-table', 'bleed');
    $('statistics').replaceChildren(...blocks, head, grid);
  }

  /** 엑셀 다운로드 = 일별 11열(최신순, 화면과 같은 서식)을 UTF-8 BOM CSV로. */
  function downloadStatistics() {
    const data = state.statistics;
    if (!data) return;
    // 값이 없는 칸(분모 0)은 빈 칸이다 — 화면의 '-'는 선두 '-'라 셀 규칙이 '로 감싸 엑셀에 '-로 보인다.
    saveCsv(`statistics_${data.period.since}_${data.period.until}.csv`, [
      STAT_COLUMNS.map((column) => column.label),
      ...data.daily.map((row) => STAT_COLUMNS.map((column) => (row[column.key] == null ? '' : (column.csv || column.format)(row[column.key])))),
    ]);
  }


  const manage = initManage({ api, el, table, clock, valueText, state, openDialog, showRecord, reload: () => loaders[state.tab]() });

  // 기간 바: 프리셋은 누르면 바로 조회, '직접'은 날짜 칸의 조회 버튼으로.
  for (const [name, load] of [['dashboard', loadDashboard], ['statistics', loadStatistics]]) {
    for (const button of $(`${name}-filters`).querySelectorAll('[data-range]')) button.onclick = () => {
      setPeriod(name, button.dataset.range);
      if (button.dataset.range !== 'custom') { $('app-error').hidden = true; load(); }
    };
    $(`${name}-filters`).onsubmit = (event) => { event.preventDefault(); $('app-error').hidden = true; load(); };
  }
  for (const button of $('statistics-views').querySelectorAll('[data-view]')) button.onclick = () => {
    state.statisticsView = button.dataset.view;
    for (const other of $('statistics-views').children) other.setAttribute('aria-pressed', String(other === button));
    renderStatistics();
  };
  $('record-close').onclick = () => $('record-dialog').close();
  $('record-dialog').onclose = () => { $('record-view').replaceChildren(); $('record-actions').replaceChildren(); };
  // 비모달 패널은 Esc를 스스로 받지 않는다 — 문서에서 받아 닫는다.
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('record-dialog').open) $('record-dialog').close(); });
  $('users-filters').onsubmit = (event) => { event.preventDefault(); applyQuery('users'); $('app-error').hidden = true; loadUsers(); };
  // 검색칸(type=search): Enter로 검색, 지우기(✕)로 비우면 곧바로 전체를 다시 읽는다. 대화 폼은 날짜 칸이 있어
  // 브라우저의 Enter 암묵 제출이 막히므로 Enter도 여기서 받는다.
  for (const [input, form] of [['q', 'filters'], ['users-q', 'users-filters']]) {
    $(input).addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $(form).requestSubmit(); } });
    $(input).addEventListener('search', () => { if (!$(input).value) $(form).requestSubmit(); });
  }

  const loaders = { dashboard: loadDashboard, statistics: loadStatistics, sessions: loadSessions, users: loadUsers, starters: loadStarters, admins: manage.loadAdmins };

  function switchTab(tab) {
    state.tab = tab;
    for (const name of Object.keys(loaders)) $(name + '-pane').hidden = tab !== name;
    $('record-dialog').close();  // 패널은 연 화면의 행을 보여 준다 — 화면을 떠나면 닫는다
    if (tab === 'sessions') showDetail(false);
    $('app').classList.remove('nav-open');
    $('menu-toggle').setAttribute('aria-expanded', 'false');
    for (const name of Object.keys(loaders)) $('tab-' + name).setAttribute('aria-pressed', String(name === tab));
    $('app-error').hidden = true;
    return loaders[tab]();
  }

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

  $('logout').onclick = async () => {
    $('logout').disabled = true;
    try { await api('/admin/api/logout', { method: 'POST' }); location.replace('/admin'); }
    catch (error) { showError(`로그아웃 요청에 실패했습니다: ${error.message}`); $('logout').disabled = false; }
  };
  $('password-change').onclick = () => manage.openPasswordChange();

  $('filters').onsubmit = (e) => { e.preventDefault(); applyQuery('sessions'); $('app-error').hidden = true; loadSessions(); };
  /** 대화 조회 조건을 기본(전체·검색어 없음)으로 — 회원 화면의 '대화 보기'가 회원번호만 걸 때 쓴다. */
  function clearSessionFilters() {
    for (const input of $('filters').querySelectorAll('input')) input.value = '';
    for (const button of $('sessions-range').children) button.setAttribute('aria-pressed', String(!button.dataset.days));
    $('sessions-custom').hidden = true;
  }
  // 기간 세그먼트는 갱신 시각의 어드민 시간대 날짜 칸을 채우고 바로 조회한다. '직접'은 날짜 칸을 연다.
  for (const button of $('sessions-range').children) button.onclick = () => {
    press($('sessions-range'), button);
    const days = button.dataset.days;
    $('sessions-custom').hidden = days !== 'custom';
    if (days === 'custom') return;
    [$('since').value, $('until').value] = days ? presetRange(days) : ['', ''];
    $('filters').requestSubmit();
  };
  $('sessions-csv').prepend($('download-icon').content.firstElementChild.cloneNode(true));
  $('sessions-csv').onclick = downloadSessions;
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
      if (error.status !== 401) { $('gate-error').textContent = error.message; $('gate-error').hidden = false; }
      return;
    }
    state.role = me.role;
    state.roles = me.roles;
    state.username = me.username;
    // 시간대가 정해진 뒤에야 날짜 칸 기본값(오늘 기준 프리셋)과 화면 라벨을 채울 수 있다.
    state.tz = me.timezone;
    for (const node of document.querySelectorAll('.tz')) node.textContent = state.tz.label;
    setPeriod('dashboard', '7');
    setPeriod('statistics', '7');
    $('account').replaceChildren(document.createTextNode(me.username), el('small', null, me.role));
    $('toolbar-owner').hidden = me.role !== 'owner';
    $('gate').hidden = true;
    for (const id of ['sidebar', 'mobile-bar', 'main']) $(id).hidden = false;
    await Promise.all([loadOverview(), switchTab(state.tab)]);
  }
  start();
