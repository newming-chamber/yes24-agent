import { initManage } from "/static/lib/admin_manage.js?v=1";
import { initCharts } from "/static/lib/admin_charts.js?v=1";

  const $ = (id) => document.getElementById(id);
  const state = { page: 0, selected: null, tab: 'dashboard', dataPage: 0, datasets: [], applied: {}, currentSession: null, exportSnapshot: null, savedQueries: [], role: null, roles: [] };
  const queryForms = {
    sessions: {form: 'filters', page: 'page', fields: {q: 'q', since: 'since', until: 'until'}},
    data: {form: 'data-filters', page: 'dataPage', fields: {dataset: 'dataset', q: 'data-q', since: 'data-since', until: 'data-until', sort: 'data-sort', direction: 'data-direction', status: 'data-status', app_name: 'data-app', status_null: 'data-status-null'}},
  };
  const pending = new Map();

  /** 텍스트를 노드로 넣어 항상 이스케이프한다(대화 본문에 마크업이 섞여도 안전). */
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const statusLabel = (status) => ({ completed: '완료', failed: '실패', interrupted: '중단', unknown: '종료 상태 미확인', up: '좋아요', down: '싫어요', '1': '활성', '0': '비활성', running: '진행 중', ok: '성공' })[status] || status;
  const num = (n) => (n ?? 0).toLocaleString('ko-KR');
  const bytes = (b) => {
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0, v = b || 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  };
  /** epoch 초 → 로컬 시각 문자열. */
  const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleString('ko-KR', { timeZone: 'UTC' }) + ' UTC' : '');

  /** 오류 문구는 서버 `detail`이 정본이다 — 상태코드별 문구를 여기서 다시 만들지 않는다. */
  async function api(path, {responseType, ...options} = {}) {
    const res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options });
    // 로그인한 화면에서 세션이 끊기면 페이지를 통째로 새로 연다 — 화면 상태를 손으로 비우지 않는다.
    if (res.status === 401 && state.role) { location.replace('/admin'); throw new DOMException('', 'AbortError'); }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join(' · ') : body.detail;
      throw Object.assign(new Error(detail || `요청 실패 (${res.status})`), { status: res.status, body, response: res });
    }
    return responseType === 'response' ? res : res.status === 204 ? null : res.json();
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
          const back = key === 'detail' ? target.querySelector('#back') : null;
          target.replaceChildren(...(back ? [back] : []), el('div', 'placeholder', '불러오지 못했습니다. 새로고침으로 다시 시도하세요.'));
        }
      }
    } finally {
      if (pending.get(key) === controller) pending.delete(key);
    }
  }

  function loadOverview() {
    return request('stats', '/admin/api/overview', (o) => {
      for (const id of ['dashboard-app', 'data-app']) {
        const selected = $(id).value;
        $(id).replaceChildren(new Option('전체 앱', ''), ...o.apps.map((app) => new Option(app.app_name, app.app_name)));
        $(id).value = selected;
      }
      const stats = [['사용자', num(o.users)], ['활성 사용자', num(o.active_users)], ['대화', num(o.sessions)], ['턴', num(o.turns)],
        ...(o.turn_statuses || []).map((s) => [statusLabel(s.status), num(s.count)]),
        ['DB 크기', bytes(o.db_bytes)]];
      $('stats').replaceChildren(...stats.map(([label, value]) => {
        const box = el('div', 'stat');
        box.append(el('b', null, value), el('span', null, label));
        return box;
      }));
      $('last-activity').textContent = o.last_activity ? `전체 기간 · 최근 활동 ${clock(o.last_activity)}` : '전체 기간 · 활동 기록 없음';
    });
  }

  function updatePager(data, prefix, page) {
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    $(prefix + 'page-info').textContent = `${page + 1} / ${pages} · 총 ${num(data.total)}건`;
    $(prefix + 'prev').disabled = page <= 0;
    $(prefix + 'next').disabled = page + 1 >= pages;
  }

  const sessionKey = (s) => JSON.stringify([s.app_name, s.user_id, s.id]);

  function loadSessions() {
    const page = state.page;
    const params = new URLSearchParams({ ...state.applied.sessions, page });
    $('sessions').replaceChildren(el('div', 'placeholder', '대화를 불러오는 중…'));
    $('prev').disabled = $('next').disabled = true;
    return request('sessions', `/admin/api/sessions?${params}`, (data) => {
      $('sessions').replaceChildren(...data.items.map((s) => {
        const row = el('button', 'session');
        row.dataset.key = sessionKey(s);
        row.setAttribute('aria-pressed', String(row.dataset.key === state.selected));
        row.onclick = () => selectSession(s);
        const preview = el('div', `preview${s.preview ? '' : ' empty'}`, s.preview || '(사용자 발화 없음)');
        const meta = el('div', 'meta');
        meta.append(el('span', 'sid', s.id.slice(0, 12)), el('span', null, `${num(s.turn_count)}턴 · ${clock(s.update_time)}`));
        row.append(preview, meta);
        return row;
      }));
      if (!data.items.length) $('sessions').replaceChildren(el('div', 'placeholder', '조건에 맞는 대화가 없습니다. 검색 조건을 초기화해 보세요.'));
      updatePager(data, '', page);
      $('sessions').scrollTop = 0;
    });
  }

  // ── 세션 상세 ───────────────────────────────────────────────────────────
  /** 조사 과정(chat_turn.process.steps) — 도구 호출·결과가 스텝으로 남아 있다. */
  function renderSteps(steps) {
    const box = el('details', 'part');
    box.dataset.kind = 'steps';
    box.append(el('summary', null, `조사 과정 · ${num(steps.length)}스텝`));
    const list = el('ol', 'steps');
    for (const st of steps) {
      const item = el('li', st.state === 'failed' ? 'failed' : null);
      item.append(el('span', 'stage', `r${st.round} ${st.stage}`), document.createTextNode(st.detail || ''));
      if (st.sources?.length) item.append(document.createTextNode(` — ${st.sources.map((x) => x.title || x.url).join(' · ')}`));
      list.append(item);
    }
    box.append(list);
    return box;
  }

  function renderSources(sources) {
    const box = el('details', 'part');
    box.dataset.kind = 'sources';
    box.append(el('summary', null, `인용 출처 ${num(sources.length)}건`));
    const list = el('div', 'sources');
    for (const s of sources) {
      const link = el('a', null, `[${s.id}] ${s.title || s.url}`);
      // 출처 URL은 웹 검색 결과에서 온 비신뢰 데이터다. scheme을 http/https로 한정하지 않으면
      // `javascript:` URI가 섞였을 때 운영자 클릭이 admin 오리진에서 스크립트를 실행한다.
      link.href = typeof s.url === 'string' && /^https?:\/\//i.test(s.url) ? s.url : '#';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const line = el('div');
      line.append(link);
      list.append(line);
    }
    box.append(list);
    return box;
  }

  /** 턴 하나 = 사용자 발화 상자 + 답변 상자(상태·지연·토큰·피드백·과정·출처). */
  function renderTurn(t) {
    const user = el('div', 'event user');
    const asked = el('div', 'who');
    asked.append(el('span', null, 'user'), el('span', null, clock(t.asked_at)));
    user.append(asked, el('div', 'body', t.user_message));

    const bot = el('div', 'event');
    user.dataset.turnId = bot.dataset.turnId = t.turn_id || '';
    const identifier = el('details', 'part');
    identifier.append(el('summary', null, '턴 식별자'), el('span', null, t.turn_id || '식별자 없음'));
    if (t.turn_id) {
      const copy = el('button', null, '식별자 복사');
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(t.turn_id); copy.textContent = '복사됨'; }
        catch { copy.textContent = '복사 실패 · 텍스트를 선택해 복사하세요'; }
      };
      identifier.append(copy);
    }
    bot.append(identifier);
    const who = el('div', 'who');
    who.append(el('span', t.status === 'completed' ? null : 'failed', statusLabel(t.status)));
    if (!t.history_saved) who.append(el('span', null, '구 이벤트 재조립'));
    if (t.elapsed_ms !== null) who.append(el('span', null, `${(t.elapsed_ms / 1000).toFixed(1)}s`));
    if (t.attributable_total_tokens) who.append(el('span', null, `${num(t.attributable_total_tokens)} tok`));
    if (t.rbti_applied) who.append(el('span', null, `RBTI ${t.rbti_applied}`));
    if (t.likes || t.dislikes) who.append(el('span', null, `👍 ${num(t.likes)} 👎 ${num(t.dislikes)}`));
    if (t.clicks) who.append(el('span', null, `클릭 ${num(t.clicks)}`));
    bot.append(who);
    if (t.error) bot.append(el('div', 'failed', `${t.error.code}: ${t.error.message}`));
    bot.append(el('div', 'body', t.assistant_message));
    if (t.process?.steps?.length) bot.append(renderSteps(t.process.steps));
    if (t.sources?.length) bot.append(renderSources(t.sources));
    return [user, bot];
  }

  function selectSession(session, { preservePosition = false } = {}) {
    const scrollTop = $('detail').scrollTop;
    state.currentSession = { ...session };
    state.selected = sessionKey(session);
    $('main').classList.add('has-selection');
    for (const row of $('sessions').querySelectorAll('.session')) row.setAttribute('aria-pressed', String(row.dataset.key === state.selected));
    const back = el('button', null, '← 대화 목록');
    back.id = 'back';
    back.onclick = () => $('main').classList.remove('has-selection');
    $('detail').replaceChildren(back, el('div', 'placeholder', '대화를 불러오는 중…'));
    const params = new URLSearchParams({ app_name: session.app_name, user_id: session.user_id });
    return request('detail', `/admin/api/sessions/${encodeURIComponent(session.id)}?${params}`, (d) => {
    const head = el('div', 'detail-head');
    head.append(el('h2', null, d.session.id));
    const chips = el('div', 'chips');
    const m = d.metrics;
    chips.append(
      el('span', 'chip', `${d.session.app_name} / ${d.session.user_id}`),
      el('span', 'chip', `생성 ${clock(d.session.create_time)}`),
      el('span', 'chip', `갱신 ${clock(d.session.update_time)}`),
      el('span', 'chip', `${num(m.turns)}턴`),
    );
    if (m.avg_turn_seconds !== null) chips.append(el('span', 'chip', `턴당 평균 ${m.avg_turn_seconds}s`));
    head.append(chips);

    $('detail').replaceChildren(back, head, el('h3', null, '대화 타임라인'), ...d.turns.flatMap(renderTurn));
    if (session.turn_id) {
      const targets = [...$('detail').querySelectorAll('[data-turn-id]')].filter((node) => node.dataset.turnId === session.turn_id);
      targets.forEach((node) => node.classList.add('selected-turn'));
      if (targets.length) targets[0].scrollIntoView({block: 'center'});
      else head.append(el('p', 'error', `선택한 턴(${session.turn_id})을 이 대화에서 찾을 수 없습니다.`));
    } else $('detail').scrollTop = preservePosition ? scrollTop : 0;
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
        const td = el('td', null, value);
        td.title = value;
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

  const charts = initCharts({ template: $('chart-template'), el, table, num });
  const usd = (value) => value == null ? '—' : `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: value && Math.abs(value) < 1 ? 4 : 2 })}`;
  const compact = (value) => value == null ? '미측정' : Number(value).toLocaleString('ko-KR', { notation: 'compact', maximumFractionDigits: 1 });
  const percent = (value) => value == null ? '미측정' : `${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%`;
  const points = (value) => `${value.toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%p`;
  const failRate = (s) => s?.turns ? (s.failed + s.interrupted) / s.turns * 100 : null;
  const ungroundedRate = (s) => s?.main_rows ? s.ungrounded_suspect / s.main_rows * 100 : null;
  const WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일'];

  /** 조회 기간의 모든 UTC 날짜 — API는 기록 있는 날만 주므로 빈 날을 여기서 채운다(추이에선 빈 날도 정보). */
  function periodDays(period) {
    const days = [];
    for (const day = new Date(`${period.since}T00:00:00Z`); day <= new Date(`${period.until}T00:00:00Z`); day.setUTCDate(day.getUTCDate() + 1)) days.push(day.toISOString().slice(0, 10));
    return days;
  }

  /** 증감 문구: 부호 + 절대 변화 (변화율). 이전 값이 0이거나 미측정이면 변화율은 N/A. */
  function change(current, previous, format) {
    if (current == null || previous == null) return 'N/A · 미측정';
    const difference = current - previous;
    const sign = difference > 0 ? '+' : difference < 0 ? '−' : '';
    // 비율 지표(%p)에 변화율을 또 붙이면 '퍼센트의 퍼센트'라 읽히지 않는다.
    if (format === points) return `${sign}${format(Math.abs(difference))}`;
    const rate = !difference ? '0%' : previous === 0 ? 'N/A · 이전 값 0' : `${sign}${Math.abs(difference / previous * 100).toLocaleString('ko-KR', { maximumFractionDigits: 1 })}%`;
    // 변화가 표시 자릿수 아래로 반올림되면('+$0.00') 절대값은 거짓 정보라 변화율만 남긴다.
    const absolute = format(Math.abs(difference));
    return difference && absolute === format(0) ? rate : `${sign}${absolute} (${rate})`;
  }

  /** KPI 타일. upIsBad면 증가를 빨강·감소를 초록으로 — 부호 글자가 함께 가서 색만으로 읽지 않는다. */
  function kpi(label, value, { current, previous, format, upIsBad, sub } = {}) {
    const box = el('div', 'kpi');
    box.append(el('span', null, label), el('strong', null, value));
    if (format) {
      const worse = upIsBad && current != null && previous != null && current !== previous ? (current > previous ? ' bad' : ' good') : '';
      box.append(el('span', `delta${worse}`, `${change(current, previous, format)} · 이전 기간 대비`));
    }
    if (sub) box.append(el('span', 'kpi-sub', sub));
    return box;
  }

  function loadDashboard() {
    const params = new URLSearchParams();
    for (const [id, key] of [['dashboard-since', 'since'], ['dashboard-until', 'until'], ['dashboard-app', 'app_name']]) if ($(id).value) params.set(key, $(id).value);
    // 재조회는 이전 렌더를 흐리게 유지한다 — 자리표시자로 갈아 끼우면 화면이 튄다.
    if ($('dashboard').childElementCount) $('dashboard').classList.add('is-loading');
    else $('dashboard').replaceChildren(el('div', 'placeholder', '운영 지표를 불러오는 중…'));
    return request('dashboard', `/admin/api/analytics?${params}`, renderDashboard)
      .finally(() => { if (!pending.has('dashboard')) $('dashboard').classList.remove('is-loading'); });
  }

  function renderDashboard(data) {
    const summary = data.summary, prior = data.comparison?.summary;
    const days = periodDays(data.period);
    const byDay = new Map(data.daily.map((row) => [row.day, row]));
    const daily = (pick, missing) => days.map((day) => byDay.has(day) ? pick(byDay.get(day)) : missing);
    const { krw_per_usd: krw, krw_as_of: krwAsOf } = data.currency;

    const kpis = el('div', 'kpis');
    kpis.append(
      kpi('비용 (USD 추정)', usd(summary.cost_usd), { current: summary.cost_usd, previous: prior?.cost_usd, format: usd, sub: krw && summary.cost_usd != null ? `≈ ₩${num(Math.round(summary.cost_usd * krw))} · 환율 ${krwAsOf ?? '기준일 미기재'} 기준` : '원화 환율 미설정' }),
      kpi('대화 턴', metric(summary.turns), { current: summary.turns, previous: prior?.turns, format: num }),
      kpi('과금 턴당 비용', usd(summary.cost_per_turn_usd), { current: summary.cost_per_turn_usd, previous: prior?.cost_per_turn_usd, format: usd, sub: `/ ${num(summary.priced_rows)} 과금 턴` }),
      kpi('실패·중단율', percent(failRate(summary)), { current: failRate(summary), previous: failRate(prior), format: points, upIsBad: true, sub: `실패 ${num(summary.failed)} · 중단 ${num(summary.interrupted)}턴` }),
      kpi('무접지 의심', percent(ungroundedRate(summary)), { current: ungroundedRate(summary), previous: ungroundedRate(prior), format: points, upIsBad: true, sub: `${num(summary.ungrounded_suspect)} / ${num(summary.main_rows)}턴` }),
      kpi('피드백', `👍 ${num(summary.likes)} · 👎 ${num(summary.dislikes)}`, { sub: `${prior ? `이전 기간 👍 ${num(prior.likes)} · 👎 ${num(prior.dislikes)} · ` : ''}출처 클릭 ${num(summary.clicks)}` }),
    );

    // 모델 → 색 슬롯은 API `models`의 위치(config 선언 순서라 기간과 무관하게 고정). 슬롯을 넘는 모델은 '기타'.
    // 차트엔 이 기간에 금액이 잡힌 모델만 싣는다 — 색은 위치에서 오므로 빠진 모델이 남은 색을 바꾸지 않는다.
    const costRows = data.cost.by_model_component, slots = charts.seriesSlots();
    const priced = new Set(costRows.filter((row) => row.cost_usd != null).map((row) => row.model));
    const shown = data.models.filter((model) => priced.has(model));
    // 그날 비용이 null(전부 단가 미등록)이면 모델 값도 null — 0으로 바꾸면 표·툴팁이 '$0.00'을 말한다.
    const modelCost = (names) => daily((row) => row.cost_usd == null ? null : names.reduce((sum, model) => sum + (row.cost_by_model?.[model] ?? 0), 0), 0);
    const slotted = shown.filter((model) => data.models.indexOf(model) < slots), rest = shown.filter((model) => !slotted.includes(model));
    const costSeries = slotted.map((model) => ({ label: model, className: `series-${data.models.indexOf(model) + 1}`, values: modelCost([model]) }));
    if (rest.length) costSeries.push({ label: `기타 ${num(rest.length)}개 모델`, className: 'series-other', values: modelCost(rest) });
    const costChart = charts.stackedBars({
      title: '일별 비용 · 모델별', categories: days, series: costSeries, unit: 'usd', format: usd,
      notes: { summary: '추정치 · 자세히', lines: data.cost.notes.map((item) => item.text) },
    });
    const statusChart = charts.stackedBars({
      title: '일별 턴 상태', categories: days, unit: 'count', format: (value) => `${num(value)}턴`,
      note: '실패·중단을 기준선 쪽에 쌓아 날짜끼리 높이를 비교합니다.',
      series: [['failed', 'status-critical'], ['interrupted', 'status-warning'], ['unknown', 'status-secondary'], ['completed', 'status-muted']]
        .map(([key, className]) => ({ label: statusLabel(key), className, values: daily((row) => row[key], 0) })),
    });
    const tokenChart = charts.stackedBars({
      title: '측정 턴당 토큰 구성', categories: days, unit: 'tokens', format: compact,
      note: '메인 에이전트 기록의 일별 토큰 ÷ 토큰이 측정된 턴 수입니다. 서브콜 토큰은 비용 차트에만 들어갑니다. 비용이 오른 날이 턴 수 때문인지 턴당 토큰 때문인지 위 두 차트와 나란히 봅니다.',
      series: [['input_uncached', '입력 (캐시 제외)', 'series-1'], ['input_cached', '캐시 입력', 'series-3'], ['output_billed', '출력 (사고 포함)', 'series-2']]
        .map(([key, label, className]) => ({ label, className, values: daily((row) => row.measured_main_rows ? (row.main_tokens?.[key] ?? 0) / row.measured_main_rows : 0, 0) })),
    });
    const latencyChart = charts.lines({
      title: '응답 지연', categories: days, format: seconds,
      note: `측정된 턴만 집계합니다(최근접 순위 분위수). 기간 전체 p50 ${seconds(summary.p50_seconds)} · p95 ${seconds(summary.p95_seconds)} · 최댓값 ${seconds(summary.max_seconds)} · 표본 ${num(summary.elapsed_rows)} / ${num(summary.turns)}턴.`,
      series: [['p50_seconds', 'p50', 'series-1'], ['p95_seconds', 'p95', 'series-2']]
        .map(([key, label, className]) => ({ label, className, values: daily((row) => row.elapsed_rows ? row[key] : null, null) })),
      extra: [{ label: '측정 턴', values: daily((row) => row.elapsed_rows, 0) }],
    });
    const hours = WEEKDAYS.map(() => Array(24).fill(0));
    for (const cell of data.hourly) if (hours[cell.weekday] && cell.hour in hours[cell.weekday]) hours[cell.weekday][cell.hour] = cell.turns;
    const heatmap = charts.heatmap({ title: '요일 × 시간대', note: 'UTC 기준 턴 시작 시각입니다. KST는 +9시간.', rows: WEEKDAYS, cols: [...hours[0].keys()], values: hours, format: (value) => `${num(value)}턴` });

    const optional = (value) => value == null ? '—' : num(value);
    const costTable = analysisCard('모델 × 컴포넌트 비용', costRows.length ? table([
      {key: 'model', label: '모델'}, {key: 'component', label: '컴포넌트'}, {key: 'rows', label: '행 수'}, {key: 'llm_calls', label: 'LLM 콜', format: optional},
      {key: 'input_uncached', label: '입력 (캐시 제외)', format: metric}, {key: 'input_cached', label: '캐시 입력', format: metric}, {key: 'output_billed', label: '출력', format: metric},
      {key: 'cost_usd', label: '비용 USD', format: (value) => value == null ? '단가 미등록' : usd(value)}, {key: 'price_effective_from', label: '단가 적용일', format: (value) => value ?? '—'},
    ], costRows.map((row) => ({...row, rows: `${num(row.rows)}${row.component === 'web_grounding' ? ' 요청' : ''}`}))) : el('p', 'analysis-note', '선택한 기간에 사용량 기록이 없습니다.'));
    const userTable = analysisCard('사용자별 비용 상위', data.users.length ? table([
      {key: 'app_name', label: '앱'}, {key: 'user_id', label: '사용자'}, {key: 'priced_rows', label: '과금 턴', format: metric}, {key: 'cost_usd', label: '비용', format: usd}, {key: 'cost_per_turn_usd', label: '과금 턴당 비용', format: usd},
    ], data.users, openUsage) : el('p', 'analysis-note', '사용자에 귀속된 사용량 기록이 없습니다.'), '메인 에이전트 기록 기준입니다(서브콜은 사용자 귀속이 없음). 행을 선택하면 그 사용자의 사용량 원본을 엽니다.');
    const tables = el('div', 'analysis-grid');
    tables.append(userTable, comparisonCard(data));
    $('dashboard').replaceChildren(kpis, costChart, statusChart, tokenChart, latencyChart, heatmap, costTable, tables);
  }

  /** 사용자 행 → 데이터 탐색 `usage` 원본을 user_id 정확 일치로 연다. 조건을 먼저 채우고 탭 전환이 한 번만 조회한다. */
  async function openUsage(row) {
    await ensureDatasets();
    if (!state.datasets.some((item) => item.id === 'usage')) return;
    $('dataset').value = 'usage';
    clearDataFilters();
    const exact = $('data-exact').querySelector('[data-exact="user_id"]');
    if (exact) exact.value = row.user_id;
    $('data-app').value = row.app_name;
    $('data-since').value = $('dashboard-since').value;
    $('data-until').value = $('dashboard-until').value;
    applyQuery('data');
    switchTab('data');
  }

  const currentDataset = () => state.datasets.find((item) => item.id === $('dataset').value);

  function configureDataset() {
    const dataset = currentDataset();
    if (!dataset) return;
    $('data-sort').replaceChildren(...dataset.sort_columns.map((key) => new Option(dataset.columns.find((column) => column.key === key)?.label || key, key)));
    $('data-sort').value = dataset.default_sort;
    $('data-status').replaceChildren(new Option('전체 상태', ''), ...(dataset.status_values || []).map((status) => new Option(statusLabel(status), status)));
    $('data-status-label').hidden = !dataset.status_column;
    $('data-null-label').hidden = !dataset.nullable_status;
    $('data-status-null').value = '';
    $('data-exact').replaceChildren(...(dataset.exact_filters || []).map((key) => {
      const label = $('exact-template').content.firstElementChild.cloneNode(true);
      label.firstElementChild.textContent = `${key} 일치`;
      label.lastElementChild.dataset.exact = key;
      return label;
    }));
    $('data-app-label').hidden = !dataset.app_filter;
    for (const id of ['data-since', 'data-until']) { $(id).disabled = !dataset.date_column; if (!dataset.date_column) $(id).value = ''; }
    $('data-q').disabled = !dataset.search_columns.length;
    $('data-description').textContent = `${dataset.label} · ${dataset.date_column ? `기간 기준: ${dataset.columns.find((column) => column.key === dataset.date_column)?.label || dataset.date_column} (UTC)` : '기간 필터 없음'} · NULL은 값 없음`;
    manage.datasetTools(dataset);
    state.dataPage = 0;
  }

  async function ensureDatasets() {
    if (state.datasets.length) return;
    $('data').replaceChildren(el('div', 'placeholder', '데이터 목록을 불러오는 중…'));
    await request('data', '/admin/api/datasets', (data) => {
      state.datasets = data.items;
      $('dataset').replaceChildren(...data.items.map((item) => new Option(item.label, item.id)));
      configureDataset();
      applyQuery('data');
      readSavedQueries();
    });
  }

  async function loadDatasets() {
    await ensureDatasets();
    if (state.datasets.length) return loadData();
  }

  function loadData() {
    const applied = state.applied.data;
    const dataset = state.datasets.find((item) => item.id === applied?.dataset);
    if (!dataset) return loadDatasets();
    const page = state.dataPage;
    const {dataset: datasetId, ...filters} = applied;
    const params = new URLSearchParams({...filters, page});
    $('csv-all').disabled = $('csv').disabled = $('data-prev').disabled = $('data-next').disabled = true;
    $('data').replaceChildren(el('div', 'placeholder', '데이터를 불러오는 중…'));
    return request('data', `/admin/api/data/${encodeURIComponent(dataset.id)}?${params}`, (data) => {
      state.exportSnapshot = {page};
      $('data').replaceChildren(data.items.length ? table(dataset.columns.map((column) => ({ ...column, format: column.type === 'datetime' ? (value) => value == null ? 'NULL · 값 없음' : clock(value) : undefined })), data.items, (row) => openRecord(dataset, row)) : el('div', 'placeholder', '조건에 맞는 기록이 없습니다. 검색 조건을 초기화해 보세요.'));
      updatePager(data, 'data-', page);
      $('csv').disabled = !data.items.length;
      $('csv-all').disabled = pending.has('export');
    });
  }

  /** 기록 다이얼로그 — 데이터 행·관리자 행·관리 폼이 같은 다이얼로그를 쓴다. 폼은 #record-actions에 붙는다. */
  function openDialog(title, record) {
    $('record-title').textContent = title;
    $('record-json').textContent = record ? JSON.stringify(record, null, 2) : '';
    $('record-copy').hidden = !record;
    $('copy-status').textContent = '';
    $('record-actions').replaceChildren();
    $('record-dialog').showModal();
  }

  function openRecord(dataset, row) {
    openDialog('기록 상세', row);
    const conversation = $('record-conversation');
    conversation.hidden = !row.app_name || !row.user_id || !row.session_id;
    conversation.onclick = () => {
      const session = { id: row.session_id, app_name: row.app_name, user_id: row.user_id, turn_id: row.turn_id };
      $('record-dialog').close();
      switchTab('sessions');
      selectSession(session);
    };
    manage.recordActions(dataset, row);
  }

  function readQuery(name) {
    const values = {};
    for (const [key, id] of Object.entries(queryForms[name].fields)) {
      if ($(id).closest('label')?.hidden || $(id).disabled) continue;
      if ($(id).value.trim()) values[key] = $(id).value.trim();
    }
    // 정확 일치는 데이터셋이 선언한 키마다 한 칸이고, 채운 칸끼리 AND로 걸린다.
    if (name === 'data') for (const input of $('data-exact').querySelectorAll('input')) if (input.value.trim()) values[input.dataset.exact] = input.value.trim();
    return values;
  }

  const queryLabels = {q: '검색', since: '시작일 UTC', until: '종료일 UTC', sort: '정렬', direction: '순서', status: '상태', app_name: '앱'};

  function queryNote(name) {
    const applied = state.applied[name] || {};
    const dirty = JSON.stringify(readQuery(name)) !== JSON.stringify(applied);
    const note = $('query-note-' + name);
    const summary = Object.entries(applied).filter(([key]) => key !== 'dataset').map(([key, value]) => `${queryLabels[key] || key}: ${value}`).join(' · ') || '전체';
    note.textContent = `${dirty ? '조건 변경됨 · 검색을 눌러 적용하세요. ' : ''}조회 조건: ${summary}`;
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

  // 저장 조회는 이 브라우저의 편의 기능이다. 조건의 유효성은 서버가 판정하고(422 detail 표시),
  // 여기서는 사라진 데이터셋만 거른다.
  const savedQueryKey = 'yes24-admin-saved-queries-v1';
  const usableQuery = (query) => Boolean(query) && typeof query === 'object' && state.datasets.some((item) => item.id === query.dataset);

  function showSavedQueries() {
    $('saved-query').replaceChildren(new Option('선택하세요', ''), ...state.savedQueries.map((item, index) => new Option(item.name, String(index))));
  }

  function readSavedQueries() {
    try {
      const stored = JSON.parse(localStorage.getItem(savedQueryKey) || '[]');
      if (!Array.isArray(stored)) throw new Error('invalid');
      state.savedQueries = stored.filter((item) => item && typeof item.name === 'string' && item.name.trim() && usableQuery(item.query));
      $('saved-status').textContent = state.savedQueries.length !== stored.length ? '더 이상 없는 데이터의 저장 조건은 제외했습니다.' : '검색 조건을 이 브라우저에 저장합니다.';
    } catch {
      state.savedQueries = [];
      $('saved-status').textContent = '저장된 조건을 읽을 수 없습니다. 브라우저 저장소 설정을 확인하세요.';
    }
    showSavedQueries();
  }

  function writeSavedQueries(items) {
    try {
      localStorage.setItem(savedQueryKey, JSON.stringify(items));
      state.savedQueries = items;
      showSavedQueries();
      return true;
    } catch {
      $('saved-status').textContent = '저장할 수 없습니다. 브라우저 저장소가 차단되었거나 용량이 부족합니다.';
      return false;
    }
  }

  $('saved-save').onclick = () => {
    const name = $('saved-name').value.trim();
    if (!name) { $('saved-status').textContent = '조회 이름을 입력하세요.'; $('saved-name').focus(); return; }
    if (!usableQuery(state.applied.data)) { $('saved-status').textContent = '조회 조건을 먼저 적용하세요.'; return; }
    if (writeSavedQueries([...state.savedQueries.filter((item) => item.name !== name), {name, query: {...state.applied.data}}])) $('saved-status').textContent = `'${name}' 이름으로 적용된 조건을 저장했습니다. 같은 이름은 덮어씁니다.`;
  };
  $('saved-load').onclick = () => {
    const item = state.savedQueries[$('saved-query').value];
    if (!item) { $('saved-status').textContent = '불러올 조회를 선택하세요.'; return; }
    $('dataset').value = item.query.dataset;
    configureDataset();
    for (const [key, id] of Object.entries(queryForms.data.fields)) {
      if (key === 'app_name' && item.query[key] && ![...$(id).options].some((option) => option.value === item.query[key])) $(id).append(new Option(item.query[key], item.query[key]));
      $(id).value = item.query[key] || '';
      // 선택지에 없는 값이면 select가 비는데, 비운 채 두지 않고 데이터셋 기본값으로 되돌린다.
      if ($(id).selectedIndex < 0) { if (key === 'sort') $(id).value = currentDataset().default_sort; else $(id).selectedIndex = 0; }
    }
    for (const input of $('data-exact').querySelectorAll('input')) input.value = item.query[input.dataset.exact] || '';
    applyQuery('data');
    // 복원 판정은 폼이 실제로 받아들인 조건(메타데이터가 만든 선택지·칸)과 저장본의 차이다.
    const dropped = Object.entries(item.query).filter(([key, value]) => state.applied.data[key] !== value).map(([key, value]) => `${queryLabels[key] || key} '${value}'`);
    $('saved-name').value = item.name;
    $('saved-status').textContent = dropped.length ? `'${item.name}' 조건을 불러왔지만 저장된 ${dropped.join(' · ')}은(는) 이 데이터에 적용할 수 없어 기본값으로 불러왔습니다.` : `'${item.name}' 조건을 적용했습니다.`;
    $('app-error').hidden = true;
    loadData();
  };
  $('saved-delete').onclick = () => {
    const selected = $('saved-query').value;
    if (selected === '' || !state.savedQueries[selected]) { $('saved-status').textContent = '삭제할 조회를 선택하세요.'; return; }
    if (writeSavedQueries(state.savedQueries.filter((_, index) => index !== Number(selected)))) { $('saved-name').value = ''; $('saved-status').textContent = '저장된 조회를 삭제했습니다.'; }
  };

  /** CSV는 서버 export가 만든다 — page가 있으면 그 페이지만, 없으면 적용 조건 전체. */
  async function downloadCsv(page) {
    const {dataset: datasetId, ...filters} = state.applied.data;
    if (!datasetId || pending.has('export')) return;
    const controller = new AbortController();
    pending.set('export', controller);
    const scope = page === undefined ? '조건 전체' : `${page + 1}페이지`;
    $('csv-all').disabled = $('csv').disabled = true;
    $('csv-cancel').hidden = false;
    $('export-status').textContent = `${datasetId} · ${scope} CSV를 준비하고 있습니다.`;
    try {
      const params = new URLSearchParams(page === undefined ? filters : {...filters, page});
      const response = await api(`/admin/api/data/${encodeURIComponent(datasetId)}/export?${params}`, {signal: controller.signal, responseType: 'response'});
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = el('a');
      link.href = url;
      link.download = page === undefined ? `${datasetId}-filtered.csv` : `${datasetId}-page-${page + 1}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      $('export-status').textContent = `${datasetId} ${scope} CSV를 다운로드했습니다.`;
    } catch (error) {
      if (pending.get('export') === controller) $('export-status').textContent = error.name === 'AbortError' ? '다운로드를 취소했습니다.' : `다운로드 실패: ${error.message}`;
    } finally {
      if (pending.get('export') === controller) {
        pending.delete('export');
        $('csv-cancel').hidden = true;
        $('csv-all').disabled = $('csv').disabled = !state.exportSnapshot;
      }
    }
  }

  function comparisonCard(data) {
    if (!data.comparison) return analysisCard('이전 기간 비교', el('p', 'analysis-note', '이전 동일 기간을 계산할 수 없습니다. 시작일과 종료일을 확인하세요.'));
    const current = data.summary, prior = data.comparison.summary;
    const rows = [
      // KPI 타일에 증감이 이미 있는 지표(턴·비용·과금 턴당 비용·피드백)는 싣지 않는다.
      ['완료 턴', 'completed', metric], ['실패 턴', 'failed', metric], ['중단 턴', 'interrupted', metric], ['종료 상태 미확인', 'unknown', metric],
      ['응답시간 평균', 'elapsed_avg_seconds', seconds], ['응답시간 p95', 'p95_seconds', seconds],
    ].map(([label, key, format]) => ({label, previous: format(prior[key]), current: format(current[key]), change: change(current[key], prior[key], format)}));
    return analysisCard('이전 동일 기간 비교', table([{key: 'label', label: '지표'}, {key: 'previous', label: '이전 기간'}, {key: 'current', label: '조회 기간'}, {key: 'change', label: '증감 · 변화율'}], rows), `이전 기간 ${data.comparison.period.since} ~ ${data.comparison.period.until} UTC. 증감은 조회 기간 − 이전 기간입니다.`);
  }

  const manage = initManage({ api, el, table, clock, valueText, state, openDialog, reloadData: () => loadData() });

  /** 기간 프리셋은 날짜 칸만 채운다 — 조회는 사용자가 누른다. */
  const setRange = (days) => {
    const day = new Date();
    $('dashboard-until').value = day.toISOString().slice(0, 10);
    day.setUTCDate(day.getUTCDate() - days + 1);
    $('dashboard-since').value = day.toISOString().slice(0, 10);
  };
  setRange(7);
  for (const button of $('dashboard-filters').querySelectorAll('[data-days]')) button.onclick = () => setRange(Number(button.dataset.days));
  $('dashboard-filters').onsubmit = (event) => { event.preventDefault(); $('app-error').hidden = true; loadDashboard(); };
  $('data-filters').onsubmit = (event) => { event.preventDefault(); applyQuery('data'); $('app-error').hidden = true; loadData(); };
  function clearDataFilters() {
    for (const input of $('data-filters').querySelectorAll('input')) input.value = '';
    for (const id of ['data-status', 'data-app', 'data-status-null']) $(id).value = '';
    $('data-direction').value = 'desc';
    configureDataset();
    applyQuery('data');
    $('app-error').hidden = true;
  }
  $('data-filters').onreset = (event) => { event.preventDefault(); clearDataFilters(); if (state.role) loadData(); };
  $('dataset').onchange = () => $('data-filters').reset();
  $('data-prev').onclick = () => { if (state.dataPage > 0) { state.dataPage--; loadData(); } };
  $('data-next').onclick = () => { state.dataPage++; loadData(); };
  $('record-close').onclick = () => $('record-dialog').close();
  $('record-dialog').onclose = () => { $('record-conversation').onclick = null; $('record-conversation').hidden = true; $('record-json').textContent = ''; $('copy-status').textContent = ''; $('record-actions').replaceChildren(); };
  $('record-copy').onclick = async () => {
    try { await navigator.clipboard.writeText($('record-json').textContent); $('copy-status').textContent = 'JSON을 복사했습니다.'; }
    catch { $('copy-status').textContent = '복사할 수 없습니다. 아래 JSON을 선택해 복사하세요.'; }
  };
  $('csv').onclick = () => downloadCsv(state.exportSnapshot?.page);
  $('csv-all').onclick = () => downloadCsv();
  $('csv-cancel').onclick = () => pending.get('export')?.abort();
  $('data-status-null').onchange = () => { if ($('data-status-null').value) $('data-status').value = ''; queryNote('data'); };
  $('data-status').onchange = () => { if ($('data-status').value) $('data-status-null').value = ''; queryNote('data'); };

  const loaders = { dashboard: loadDashboard, data: loadDatasets, sessions: loadSessions, admins: manage.loadAdmins };

  function switchTab(tab) {
    state.tab = tab;
    $('main').classList.remove('has-selection');
    $('list-pane').hidden = $('detail').hidden = tab !== 'sessions';
    for (const name of ['dashboard', 'data', 'admins']) $(name + '-pane').hidden = tab !== name;
    for (const name of Object.keys(loaders)) $('tab-' + name).setAttribute('aria-pressed', String(name === tab));
    $('app-error').hidden = true;
    return loaders[tab]();
  }

  $('gate').onsubmit = async (e) => {
    e.preventDefault();
    const button = $('gate').querySelector('button');
    button.disabled = true;
    $('gate-error').hidden = true;
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
    } finally { button.disabled = false; }
  };

  $('logout').onclick = async () => {
    $('logout').disabled = true;
    try { await api('/admin/api/logout', { method: 'POST' }); location.replace('/admin'); }
    catch (error) { showError(`로그아웃 요청에 실패했습니다: ${error.message}`); $('logout').disabled = false; }
  };
  $('password-change').onclick = () => manage.openPasswordChange();

  $('filters').onsubmit = (e) => { e.preventDefault(); applyQuery('sessions'); $('app-error').hidden = true; loadSessions(); };
  $('filters').onreset = (event) => {
    event.preventDefault();
    for (const input of $('filters').querySelectorAll('input')) input.value = '';
    applyQuery('sessions');
    $('app-error').hidden = true;
    if (state.role) loadSessions();
  };
  $('prev').onclick = () => { if (state.page > 0) { state.page--; loadSessions(); } };
  $('next').onclick = () => { state.page++; loadSessions(); };
  for (const tab of Object.keys(loaders)) $('tab-' + tab).onclick = () => switchTab(tab);
  $('refresh').onclick = () => {
    $('app-error').hidden = true;
    loadOverview();
    if (state.tab === 'sessions') {
      const detailVisible = $('main').classList.contains('has-selection');
      loadSessions();
      if (state.currentSession) {
        selectSession(state.currentSession, {preservePosition: true});
        if (!detailVisible) $('main').classList.remove('has-selection');
      }
    } else loaders[state.tab]();
  };

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
    $('account').textContent = `${me.username} · ${me.role}`;
    $('tab-admins').hidden = me.role !== 'owner';
    $('gate').hidden = true;
    for (const id of ['header', 'toolbar', 'main']) $(id).hidden = false;
    await Promise.all([loadOverview(), switchTab(state.tab)]);
  }
  start();
