/**
 * 어드민 분석 차트 — 무의존 SVG(설계 docs/admin-analytics-design-20260914.md §4·§5.1).
 * CSP `default-src 'self'` 아래에서 그린다: 위치·크기는 SVG 속성, 색은 CSS 클래스(`--c`),
 * 글자는 textContent. SVG 네임스페이스는 템플릿에서 파생한다(URL 리터럴 가드).
 * 렌더 함수는 DOM만 만든다 — 데이터 조회·색 배정은 호출자(admin.js) 몫이다.
 */

// 기하(px). 글자 크기는 CSS가 고정하고, 확대 대신 컨테이너 폭으로 다시 그린다.
const TOP = 10, PLOT_H = 180, AXIS_H = 24, LEFT = 56, RIGHT = 16, BAR_MAX = 24, GAP = 2, RADIUS = 4, LINE_H = 16;
// 좁은 폭에서 x축 날짜 라벨(MM-DD)이 차지하는 최소 칸, 히트맵 시간 라벨의 최소 칸.
const DAY_LABEL_W = 44, HOUR_LABEL_W = 30;
const EMPTY = '선택한 기간에 기록이 없습니다.';
const KEYS = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };

/** CSS 토큰 `--{name}-1..n`의 n. 색 단계 수의 정본은 admin.css이고 JS는 세기만 한다(짝 표류 방지). */
const tokenSteps = (name) => {
  const style = getComputedStyle(document.documentElement);
  let steps = 0;
  while (style.getPropertyValue(`--${name}-${steps + 1}`).trim()) steps++;
  return steps;
};

export function initCharts({ template, el, table, num }) {
  const svgNS = template.content.firstElementChild.namespaceURI;

  const node = (parent, tag, attrs, cls, text) => {
    const child = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attrs || {})) child.setAttribute(key, String(value));
    if (cls) child.setAttribute('class', cls);
    if (text !== undefined) child.textContent = text;
    parent.append(child);
    return child;
  };
  const size = (svg, width, height) => {
    svg.replaceChildren();
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
  };

  /** 0에서 시작하는 깨끗한 눈금(1·2·2.5·5 × 10^k). 건수는 정수 간격. */
  function scale(max, integer) {
    if (!(max > 0)) return { top: 1, ticks: [0, 1] };
    const raw = max / 4, magnitude = 10 ** Math.floor(Math.log10(raw));
    let step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw);
    if (integer) step = Math.max(1, Math.ceil(step));
    const count = Math.ceil(max / step);
    return { top: count * step, ticks: Array.from({ length: count + 1 }, (_, i) => i * step) };
  }

  /** 카드 뼈대: 제목 · 범례 · 그림 · 각주 · 표 토글. draw(svg, width)는 폭이 바뀔 때마다 다시 부른다. */
  function card({ title, note, notes, legend, empty, tableView, draw }) {
    const figure = el('figure', 'analysis-card chart-card');
    figure.append(el('h3', null, title));
    if (empty) figure.append(el('p', 'analysis-note', EMPTY));
    else {
      if (legend.length) {
        const list = el('ul', 'chart-legend');
        for (const item of legend) {
          const entry = el('li');
          entry.append(el('span', `swatch ${item.line ? 'key-line ' : ''}${item.className}`), document.createTextNode(item.label));
          list.append(entry);
        }
        figure.append(list);
      }
      const plot = el('div', 'chart-plot');
      const svg = template.content.firstElementChild.cloneNode(true);
      plot.append(svg);
      figure.append(plot);
      let width = 0;
      const observer = new ResizeObserver(() => {
        // 대시보드가 다시 그려져 카드가 빠지면 관찰을 끝낸다(첫 관찰은 붙기 전일 수 있다).
        if (!figure.isConnected) { if (width) observer.disconnect(); return; }
        if (plot.clientWidth > 0 && plot.clientWidth !== width) { width = plot.clientWidth; draw(svg, width); }
      });
      observer.observe(plot);
    }
    if (note) figure.append(el('p', 'analysis-note', note));
    // 긴 각주 목록은 한 줄 요약 뒤로 접는다 — 각주가 차트보다 먼저 읽히지 않게.
    if (notes?.lines.length) {
      const details = el('details', 'chart-notes');
      details.append(el('summary', 'analysis-note', notes.summary), ...notes.lines.map((line) => el('p', 'analysis-note', line)));
      figure.append(details);
    }
    if (!empty) {
      const details = el('details', 'chart-table');
      details.append(el('summary', null, '표로 보기'), tableView());
      figure.append(details);
    }
    return figure;
  }

  /** y 눈금 라벨을 먼저 재서 왼쪽 여백을 정한다. 시계열 차트끼리 x축이 맞도록 LEFT가 하한. */
  function yAxis(svg, width, right, ticks, top, format) {
    const labels = ticks.map((tick) => node(svg, 'text', { 'text-anchor': 'end' }, null, format(tick)));
    const left = Math.max(LEFT, Math.ceil(Math.max(...labels.map((label) => label.getComputedTextLength()))) + 10);
    const y = (value) => TOP + PLOT_H - (value / top) * PLOT_H;
    ticks.forEach((tick, i) => {
      const at = Math.round(y(tick)) + 0.5;
      node(svg, 'line', { x1: left, x2: width - right, y1: at, y2: at }, 'grid');
      labels[i].setAttribute('x', left - 8);
      labels[i].setAttribute('y', at + 4);
    });
    return { left, y };
  }

  /** 날짜 라벨은 마지막 날부터 거꾸로 솎는다 — 가장 최근 날짜가 항상 보인다. */
  function xAxis(svg, categories, left, band) {
    const stride = Math.max(1, Math.ceil(DAY_LABEL_W / band));
    categories.forEach((category, i) => {
      if ((categories.length - 1 - i) % stride) return;
      node(svg, 'text', { x: left + band * (i + 0.5), y: TOP + PLOT_H + 16, 'text-anchor': 'middle' }, null, category.slice(5));
    });
  }

  /** 툴팁 한 개 — 값이 앞(굵게), 이름이 뒤. 시리즈 열쇠는 시리즈색 짧은 선. */
  function tip(svg, width) {
    const group = node(svg, 'g', { display: 'none' }, 'chart-tip');
    const box = node(group, 'rect', { rx: 6 });
    const lines = node(group, 'g');
    return {
      show(anchor, head, rows) {
        lines.replaceChildren();
        node(lines, 'text', { x: 10, y: LINE_H }, 'tip-head', head);
        rows.forEach((row, i) => {
          const y = LINE_H * (i + 2);
          if (row.className) node(lines, 'line', { x1: 10, x2: 20, y1: y - 4, y2: y - 4 }, `key ${row.className}`);
          const text = node(lines, 'text', { x: row.className ? 26 : 10, y });
          node(text, 'tspan', {}, 'tip-value', row.value);
          if (row.label) node(text, 'tspan', {}, null, ` ${row.label}`);
        });
        group.removeAttribute('display');
        const inner = lines.getBBox();
        const w = inner.x + inner.width + 10;
        box.setAttribute('width', w);
        box.setAttribute('height', LINE_H * (rows.length + 1) + 8);
        const x = anchor + 12 + w > width - 2 ? anchor - 12 - w : anchor + 12;
        group.setAttribute('transform', `translate(${Math.max(2, x)},${TOP})`);
      },
      hide() { group.setAttribute('display', 'none'); },
    };
  }

  /** 포인터와 키보드가 같은 인덱스를 고른다 — 막대·셀은 칸 전체가 타깃, 선은 가까운 날짜로 스냅. */
  function cursor(svg, layer, { count, pick, keys, show, hide }) {
    let index = null;
    const go = (i) => { index = Math.max(0, Math.min(count - 1, i)); layer.setAttribute('aria-label', show(index)); };
    layer.setAttribute('tabindex', '0');
    layer.setAttribute('role', 'img');
    layer.addEventListener('pointermove', (event) => {
      const box = svg.getBoundingClientRect();
      const i = pick(event.clientX - box.left, event.clientY - box.top);
      if (i === null) hide(); else go(i);
    });
    layer.addEventListener('pointerleave', () => { if (document.activeElement !== layer) hide(); });
    layer.addEventListener('focus', () => go(index ?? count - 1));
    layer.addEventListener('blur', hide);
    layer.addEventListener('keydown', (event) => {
      if (!(event.key in keys)) return;
      event.preventDefault();
      go((index ?? 0) + keys[event.key]);
    });
  }

  /** null에서 끊기는 선 경로. */
  const path = (values, x, y) => values.map((value, i) => (value == null ? '' : `${values[i - 1] == null ? 'M' : 'L'}${x(i)},${y(value)}`)).join('');

  const roundedTop = (x, top, width, bottom) => {
    const r = Math.min(RADIUS, width / 2, bottom - top);
    return `M${x},${bottom}V${top + r}Q${x},${top} ${x + r},${top}H${x + width - r}Q${x + width},${top} ${x + width},${top + r}V${bottom}Z`;
  };

  /** 날짜별 누적 막대. 첫 시리즈가 기준선에 붙는다. */
  function stackedBars({ title, note, notes, categories, series, unit, format }) {
    // null(미측정·단가 미등록)은 0이 아니다 — 막대는 그리지 않고 표·툴팁엔 포맷터의 null 표기로 간다.
    const totals = categories.map((_, i) => (series.every((item) => item.values[i] == null) ? null : series.reduce((sum, item) => sum + (item.values[i] ?? 0), 0)));
    const draw = (svg, width) => {
      size(svg, width, TOP + PLOT_H + AXIS_H);
      const { top, ticks } = scale(Math.max(...totals), unit === 'count');
      const { left, y } = yAxis(svg, width, RIGHT, ticks, top, format);
      const band = (width - left - RIGHT) / categories.length;
      // 칸보다 넓어지면 이웃 막대와 맞닿는다 — 칸이 간격보다 좁을 땐 칸의 절반만 칠한다.
      const barWidth = band - GAP >= 1 ? Math.min(BAR_MAX, band - GAP) : band / 2;
      const highlight = node(svg, 'rect', { y: TOP, width: band, height: PLOT_H, display: 'none' }, 'cursor');
      categories.forEach((_, i) => {
        const x = left + band * i + (band - barWidth) / 2;
        const last = series.findLastIndex((item) => item.values[i] > 0);
        let base = 0;
        series.forEach((item, k) => {
          const value = item.values[i] || 0;
          if (value <= 0) return;
          // 세그먼트 사이 2px 표면 간격 — 아래 세그먼트 위를 비운다.
          const upper = y(base + value), lower = y(base) - (base > 0 ? GAP : 0);
          base += value;
          if (lower - upper < 0.5) return;
          if (k === last) node(svg, 'path', { d: roundedTop(x, upper, barWidth, lower) }, `bar ${item.className}`);
          else node(svg, 'rect', { x, y: upper, width: barWidth, height: lower - upper }, `bar ${item.className}`);
        });
      });
      xAxis(svg, categories, left, band);
      const layer = node(svg, 'rect', { x: left, y: TOP, width: width - left - RIGHT, height: PLOT_H }, 'hit');
      const tooltip = tip(svg, width);
      cursor(svg, layer, {
        count: categories.length,
        keys: KEYS,
        pick: (px) => (px < left || px > width - RIGHT ? null : Math.min(categories.length - 1, Math.floor((px - left) / band))),
        show: (i) => {
          highlight.setAttribute('x', left + band * i);
          highlight.removeAttribute('display');
          const rows = [...series.filter((item) => item.values[i] > 0).map((item) => ({ value: format(item.values[i]), label: item.label, className: item.className })), { value: format(totals[i]), label: '합계' }];
          tooltip.show(left + band * (i + 0.5), `${categories[i]} UTC`, rows);
          return `${categories[i]} ${rows.map((row) => `${row.label} ${row.value}`).join(', ')}`;
        },
        hide: () => { highlight.setAttribute('display', 'none'); tooltip.hide(); },
      });
    };
    return card({
      title, note, notes, draw,
      empty: totals.every((total) => !total),
      legend: series.map(({ label, className }) => ({ label, className })),
      tableView: () => table(
        [{ key: 'category', label: '날짜 (UTC)' }, ...series.map((item, k) => ({ key: k, label: item.label, format })), { key: 'total', label: '합계', format }],
        categories.map((category, i) => ({ category, total: totals[i], ...series.map((item) => item.values[i]) })),
      ),
    });
  }

  /** 날짜별 선. null은 선을 끊는다(0으로 잇지 않음). extra는 툴팁·표에만 싣는 부가 행(표본 수). */
  function lines({ title, note, categories, series, format, extra = [] }) {
    const present = series.flatMap((item) => item.values).filter((value) => value != null);
    const lastIndex = series.map((item) => item.values.findLastIndex((value) => value != null));
    const draw = (svg, width) => {
      size(svg, width, TOP + PLOT_H + AXIS_H);
      const { top, ticks } = scale(Math.max(...present), false);
      // 끝 라벨이 서로 겹칠 만큼 가까우면 라벨을 버리고 범례·툴팁에 맡긴다(밀어 붙이지 않는다).
      const ends = series.map((item, k) => (lastIndex[k] < 0 ? null : (item.values[lastIndex[k]] / top) * PLOT_H));
      const crowded = ends.some((a, i) => a !== null && ends.some((b, j) => i < j && b !== null && Math.abs(a - b) < LINE_H));
      const endLabels = crowded ? [] : series.map((item, k) => (lastIndex[k] < 0 ? null : node(svg, 'text', {}, null, item.label)));
      const right = Math.max(RIGHT, ...endLabels.filter(Boolean).map((label) => Math.ceil(label.getComputedTextLength()) + 14));
      const { left, y } = yAxis(svg, width, right, ticks, top, format);
      const band = (width - left - right) / categories.length;
      const x = (i) => left + band * (i + 0.5);
      const crosshair = node(svg, 'line', { y1: TOP, y2: TOP + PLOT_H, display: 'none' }, 'crosshair');
      series.forEach((item, k) => {
        node(svg, 'path', { d: path(item.values, x, y) }, `line ${item.className}`);
        endLabels[k]?.setAttribute('x', x(lastIndex[k]) + 10);
        endLabels[k]?.setAttribute('y', y(item.values[lastIndex[k]]) + 4);
      });
      // 점은 선 위에, 뒤 시리즈부터 그린다. 앞 시리즈와 같은 값(p50=p95)이면 겹친 수만큼 반지름을 키워
      // 표면 링 너머로 고리가 보이게 한다 — 같은 크기로 포개면 뒤 점이 앞 점을 지운다.
      series.map((item, k) => [item, k]).reverse().forEach(([{ values, className }, k]) => values.forEach((value, i) => {
        const isolated = value != null && values[i - 1] == null && values[i + 1] == null;
        if (!isolated && i !== lastIndex[k]) return;
        const beneath = series.slice(0, k).filter((other) => other.values[i] === value).length;
        node(svg, 'circle', { cx: x(i), cy: y(value), r: 4 + 4 * beneath }, `dot ${className}`);
      }));
      xAxis(svg, categories, left, band);
      const layer = node(svg, 'rect', { x: left, y: TOP, width: width - left - right, height: PLOT_H }, 'hit');
      const tooltip = tip(svg, width);
      cursor(svg, layer, {
        count: categories.length,
        keys: KEYS,
        pick: (px) => Math.max(0, Math.min(categories.length - 1, Math.round((px - left) / band - 0.5))),
        show: (i) => {
          crosshair.setAttribute('x1', x(i));
          crosshair.setAttribute('x2', x(i));
          crosshair.removeAttribute('display');
          const rows = [...series.map((item) => ({ value: format(item.values[i]), label: item.label, className: item.className })), ...extra.map((item) => ({ value: num(item.values[i]), label: item.label }))];
          tooltip.show(x(i), `${categories[i]} UTC`, rows);
          return `${categories[i]} ${rows.map((row) => `${row.label} ${row.value}`).join(', ')}`;
        },
        hide: () => { crosshair.setAttribute('display', 'none'); tooltip.hide(); },
      });
    };
    return card({
      title, note, draw,
      empty: !present.length,
      legend: series.map(({ label, className }) => ({ label, className, line: true })),
      tableView: () => table(
        [{ key: 'category', label: '날짜 (UTC)' }, ...series.map((item, k) => ({ key: `s${k}`, label: item.label, format })), ...extra.map((item, k) => ({ key: `e${k}`, label: item.label, format: num }))],
        categories.map((category, i) => Object.fromEntries([['category', category], ...series.map((item, k) => [`s${k}`, item.values[i]]), ...extra.map((item, k) => [`e${k}`, item.values[i]])])),
      ),
    });
  }

  /** 행×열 격자. 셀 색은 최대값 기준 순차 램프(단계 수 = admin.css `--seq-*` 개수), 0은 바탕색. */
  function heatmap({ title, note, rows, cols, values, format }) {
    const max = Math.max(0, ...values.flat());
    const steps = tokenSteps('seq');
    const level = (value) => (value > 0 ? Math.min(steps, Math.ceil((value / max) * steps)) : 0);
    // 범례는 실제로 값이 들어갈 수 있는 단계만 — 정수 건수라 좁은 최대값에선 빈 단계가 생긴다.
    const legend = [{ label: format(0), className: 'seq-0' }];
    for (let k = 1; k <= steps; k++) {
      const low = Math.floor((max * (k - 1)) / steps) + 1, high = Math.floor((max * k) / steps);
      if (low <= high) legend.push({ label: low === high ? format(high) : `${num(low)}–${format(high)}`, className: `seq-${k}` });
    }
    const draw = (svg, width) => {
      size(svg, width, 1);
      const labels = rows.map((row) => node(svg, 'text', { 'text-anchor': 'end' }, null, row));
      const left = Math.ceil(Math.max(...labels.map((label) => label.getComputedTextLength()))) + 10;
      // 칸 폭은 컨테이너를 채우고, 높이는 요일 라벨 한 줄(LINE_H)~28px 사이 — 넓은 화면에서 격자가 세로로 커지지 않게.
      const cw = Math.max(8, Math.floor((width - left) / cols.length)), ch = Math.min(28, Math.max(LINE_H, cw));
      const height = TOP + rows.length * ch + AXIS_H;
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg.setAttribute('height', height);
      labels.forEach((label, r) => { label.setAttribute('x', left - 8); label.setAttribute('y', TOP + ch * (r + 0.5) + 4); });
      values.forEach((row, r) => row.forEach((value, c) => {
        node(svg, 'rect', { x: left + c * cw, y: TOP + r * ch, width: cw - GAP, height: ch - GAP, rx: 2 }, `cell seq-${level(value)}`);
      }));
      const stride = Math.ceil(HOUR_LABEL_W / cw);
      cols.forEach((col, c) => {
        if (c % stride === 0) node(svg, 'text', { x: left + c * cw + (cw - GAP) / 2, y: TOP + rows.length * ch + 14, 'text-anchor': 'middle' }, null, col);
      });
      const focus = node(svg, 'rect', { width: cw, height: ch, rx: 3, display: 'none' }, 'cell-focus');
      const layer = node(svg, 'rect', { x: left, y: TOP, width: cols.length * cw, height: rows.length * ch }, 'hit');
      const tooltip = tip(svg, width);
      cursor(svg, layer, {
        count: rows.length * cols.length,
        keys: { ...KEYS, ArrowUp: -cols.length, ArrowDown: cols.length },
        pick: (px, py) => {
          const c = Math.floor((px - left) / cw), r = Math.floor((py - TOP) / ch);
          return c < 0 || r < 0 || c >= cols.length || r >= rows.length ? null : r * cols.length + c;
        },
        show: (i) => {
          const r = Math.floor(i / cols.length), c = i % cols.length;
          focus.setAttribute('x', left + c * cw - 1);
          focus.setAttribute('y', TOP + r * ch - 1);
          focus.removeAttribute('display');
          const head = `${rows[r]} ${String(cols[c]).padStart(2, '0')}:00 UTC`;
          tooltip.show(left + c * cw + cw / 2, head, [{ value: format(values[r][c]) }]);
          return `${head} ${format(values[r][c])}`;
        },
        hide: () => { focus.setAttribute('display', 'none'); tooltip.hide(); },
      });
    };
    return card({
      title, note, draw, legend,
      empty: max === 0,
      tableView: () => table(
        [{ key: 'row', label: '요일' }, ...cols.map((col, c) => ({ key: c, label: String(col), format: num }))],
        rows.map((row, r) => ({ row, ...values[r] })),
      ),
    });
  }

  return { stackedBars, lines, heatmap, seriesSlots: () => tokenSteps('series') };
}
