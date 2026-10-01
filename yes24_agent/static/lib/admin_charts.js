/**
 * 어드민 분석 차트 — 무의존 SVG(설계 docs/admin-analytics-design-20260914.md §4·§5.1).
 * CSP `default-src 'self'` 아래에서 그린다: 위치·크기는 SVG 속성, 색은 CSS 클래스(`--c`),
 * 글자는 textContent. SVG 네임스페이스는 템플릿에서 파생한다(URL 리터럴 가드).
 * 렌더 함수는 DOM만 만든다 — 데이터 조회·색 배정은 호출자(admin.js) 몫이다.
 */

// 기하(px). 글자 크기는 CSS가 고정하고, 확대 대신 컨테이너 폭으로 다시 그린다.
const TOP = 10, PLOT_H = 180, AXIS_H = 24, LEFT = 56, RIGHT = 16, BAR_MAX = 24, GAP = 2, RADIUS = 4, LINE_H = 16;
// 좁은 폭에서 x축 날짜 라벨(MM-DD)이 차지하는 최소 칸.
const DAY_LABEL_W = 44;
const EMPTY = '선택한 기간에 기록이 없습니다.';
const KEYS = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
// 칸을 눌러 그 날짜·칸의 목록으로 갈 수 있을 때 툴팁 끝줄.
const PICK_HINT = '눌러서 대화 보기';

export function initCharts({ template, el, table }) {
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

  /** 카드 뼈대: 제목 · 범례 · 그림 · 각주 · 표 토글. draw(svg, width)는 폭이 바뀔 때마다 다시 부른다.
   *  범례 항목에 toggle이 있으면 범례가 버튼이다 — 누르면 toggle()이 참일 때 그 계열을 끄고/켜고 다시 그린다. */
  function card({ title, note, notes, legend, empty, tableView, draw }) {
    const figure = el('figure', 'analysis-card chart-card');
    let redraw = () => {};
    figure.append(el('h3', null, title));
    if (empty) figure.append(el('p', 'analysis-note', EMPTY));
    else {
      if (legend.length) {
        const list = el('ul', 'chart-legend');
        for (const item of legend) {
          const entry = el('li'), key = item.toggle ? el('button', 'legend-toggle') : entry;
          key.append(el('span', `swatch ${item.line ? 'key-line ' : ''}${item.className}`), document.createTextNode(item.label));
          if (item.toggle) {
            key.type = 'button';
            key.setAttribute('aria-pressed', 'true');
            key.title = '누르면 이 계열을 끄고 켭니다';
            key.onclick = () => { if (item.toggle()) { key.setAttribute('aria-pressed', String(key.getAttribute('aria-pressed') !== 'true')); redraw(); } };
            entry.append(key);
          }
          list.append(entry);
        }
        figure.append(list);
      }
      const plot = el('div', 'chart-plot');
      const svg = template.content.firstElementChild.cloneNode(true);
      plot.append(svg);
      figure.append(plot);
      let width = 0;
      redraw = () => { if (width) draw(svg, width); };
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
  function yAxis(svg, width, right, ticks, top, format, plotH = PLOT_H) {
    const labels = ticks.map((tick) => node(svg, 'text', { 'text-anchor': 'end' }, null, format(tick)));
    const left = Math.max(LEFT, Math.ceil(Math.max(...labels.map((label) => label.getComputedTextLength()))) + 10);
    const y = (value) => TOP + plotH - (value / top) * plotH;
    ticks.forEach((tick, i) => {
      const at = Math.round(y(tick)) + 0.5;
      node(svg, 'line', { x1: left, x2: width - right, y1: at, y2: at }, 'grid');
      labels[i].setAttribute('x', left - 8);
      labels[i].setAttribute('y', at + 4);
    });
    return { left, y };
  }

  /** 날짜 라벨은 마지막 날부터 거꾸로 솎는다 — 가장 최근 날짜가 항상 보인다. tick은 칸 라벨(기본 MM-DD). */
  function xAxis(svg, categories, left, band, tick = (category) => category.slice(5), plotH = PLOT_H) {
    const stride = Math.max(1, Math.ceil(DAY_LABEL_W / band));
    categories.forEach((category, i) => {
      if ((categories.length - 1 - i) % stride) return;
      node(svg, 'text', { x: left + band * (i + 0.5), y: TOP + plotH + 16, 'text-anchor': 'middle' }, null, tick(category));
    });
  }

  /** 툴팁 한 개 — 값이 앞(굵게), 이름이 뒤. 시리즈 열쇠는 시리즈색 짧은 선. */
  function tip(svg, width) {
    const group = node(svg, 'g', { display: 'none' }, 'chart-tip');
    const box = node(group, 'rect', { rx: 6 });
    const lines = node(group, 'g');
    return {
      show(anchor, head, rows, foot) {
        lines.replaceChildren();
        node(lines, 'text', { x: 10, y: LINE_H }, 'tip-head', head);
        if (foot) rows = [...rows, { value: '', label: foot, foot: true }];
        rows.forEach((row, i) => {
          const y = LINE_H * (i + 2);
          if (row.className) node(lines, 'line', { x1: 10, x2: 20, y1: y - 4, y2: y - 4 }, `key ${row.className}`);
          const text = node(lines, 'text', { x: row.className ? 26 : 10, y }, row.foot ? 'tip-foot' : null);
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
  /** choose가 있으면 칸을 누르거나 Enter로 그 칸을 고른다(막대 → 필터된 목록 등). */
  function cursor(svg, layer, { count, pick, keys, show, hide, choose }) {
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
    if (choose) {
      layer.classList.add('pickable');
      layer.addEventListener('click', (event) => { const box = svg.getBoundingClientRect(), i = pick(event.clientX - box.left, event.clientY - box.top); if (i !== null) choose(i); });
    }
    layer.addEventListener('keydown', (event) => {
      if (choose && event.key === 'Enter' && index !== null) { choose(index); return; }
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

  /** 날짜별 누적 막대. 첫 시리즈가 기준선에 붙는다. 날짜가 아닌 칸(요일·시각)은 tick·axisTitle로 그린다. */
  function stackedBars({ title, note, notes, categories, series, unit, format, tick, axisTitle = '날짜', onPick, pickHint = PICK_HINT }) {
    // null(미측정·단가 미등록)은 0이 아니다 — 막대는 그리지 않고 표·툴팁엔 포맷터의 null 표기로 간다.
    // 칸 이름 — tick(축 라벨 서식)이 있으면 툴팁·낭독·표도 같은 이름(예: 13 → '13시'). 날짜는 원문 그대로.
    const name = (category) => (tick ? tick(category) : category);
    const single = series.length === 1;
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
      xAxis(svg, categories, left, band, tick);
      const layer = node(svg, 'rect', { x: left, y: TOP, width: width - left - RIGHT, height: PLOT_H }, 'hit');
      const tooltip = tip(svg, width);
      cursor(svg, layer, {
        count: categories.length,
        keys: KEYS,
        pick: (px) => (px < left || px > width - RIGHT ? null : Math.min(categories.length - 1, Math.floor((px - left) / band))),
        show: (i) => {
          highlight.setAttribute('x', left + band * i);
          highlight.removeAttribute('display');
          // 계열이 하나면 값 한 줄뿐 — 계열명(제목·범례가 이미 말함)·합계 줄을 붙이면 '100질의 질의 / 100질의 합계'로 겹친다.
          const rows = single ? [{ value: format(totals[i]), className: series[0].className }]
            : [...series.filter((item) => item.values[i] > 0).map((item) => ({ value: format(item.values[i]), label: item.label, className: item.className })), { value: format(totals[i]), label: '합계' }];
          tooltip.show(left + band * (i + 0.5), name(categories[i]), rows, onPick && pickHint);
          return `${name(categories[i])} ${rows.map((row) => [row.label, row.value].filter(Boolean).join(' ')).join(', ')}`;
        },
        hide: () => { highlight.setAttribute('display', 'none'); tooltip.hide(); },
        choose: onPick && ((i) => onPick(categories[i])),
      });
    };
    return card({
      title, note, notes, draw,
      empty: totals.every((total) => !total),
      legend: series.map(({ label, legend, className }) => ({ label: legend ?? label, className })),
      tableView: () => table(
        [{ key: 'category', label: axisTitle }, ...series.map((item, k) => ({ key: k, label: item.label, format })), ...(single ? [] : [{ key: 'total', label: '합계', format }])],
        categories.map((category, i) => ({ category: name(category), total: totals[i], ...series.map((item) => item.values[i]) })),
      ),
    });
  }

  /** 날짜별 선. null은 선을 끊는다(0으로 잇지 않음). 범례를 눌러 계열을 끄면 남은 계열로 y축을 다시 잡는다
   *  (마지막 하나는 끄지 않는다). */
  function lines({ title, note, categories, series: all, format, height = PLOT_H, onPick, pickHint = PICK_HINT }) {
    const present = all.flatMap((item) => item.values).filter((value) => value != null);
    const hidden = new Set();
    const draw = (svg, width) => {
      const series = all.filter((item) => !hidden.has(item));
      const lastIndex = series.map((item) => item.values.findLastIndex((value) => value != null));
      const shown = series.flatMap((item) => item.values).filter((value) => value != null);
      const plotH = height;
      size(svg, width, TOP + plotH + AXIS_H);
      const { top, ticks } = scale(Math.max(...shown), false);
      // 끝 라벨이 서로 겹칠 만큼 가까우면 라벨을 버리고 범례·툴팁에 맡긴다(밀어 붙이지 않는다).
      const ends = series.map((item, k) => (lastIndex[k] < 0 ? null : (item.values[lastIndex[k]] / top) * plotH));
      const crowded = ends.some((a, i) => a !== null && ends.some((b, j) => i < j && b !== null && Math.abs(a - b) < LINE_H));
      const endLabels = crowded ? [] : series.map((item, k) => (lastIndex[k] < 0 ? null : node(svg, 'text', {}, null, item.label)));
      const right = Math.max(RIGHT, ...endLabels.filter(Boolean).map((label) => Math.ceil(label.getComputedTextLength()) + 14));
      const { left, y } = yAxis(svg, width, right, ticks, top, format, plotH);
      const band = (width - left - right) / categories.length;
      const x = (i) => left + band * (i + 0.5);
      const crosshair = node(svg, 'line', { y1: TOP, y2: TOP + plotH, display: 'none' }, 'crosshair');
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
      xAxis(svg, categories, left, band, undefined, plotH);
      const layer = node(svg, 'rect', { x: left, y: TOP, width: width - left - right, height: plotH }, 'hit');
      const tooltip = tip(svg, width);
      cursor(svg, layer, {
        count: categories.length,
        keys: KEYS,
        pick: (px) => Math.max(0, Math.min(categories.length - 1, Math.round((px - left) / band - 0.5))),
        show: (i) => {
          crosshair.setAttribute('x1', x(i));
          crosshair.setAttribute('x2', x(i));
          crosshair.removeAttribute('display');
          const rows = series.map((item) => ({ value: format(item.values[i]), label: item.label, className: item.className }));
          tooltip.show(x(i), categories[i], rows, onPick && pickHint);
          return `${categories[i]} ${rows.map((row) => `${row.label} ${row.value}`).join(', ')}`;
        },
        hide: () => { crosshair.setAttribute('display', 'none'); tooltip.hide(); },
        choose: onPick && ((i) => onPick(categories[i])),
      });
    };
    return card({
      title, note, draw,
      empty: !present.length,
      legend: all.map((item) => ({
        label: item.legend ?? item.label, className: item.className, line: true,
        toggle: all.length > 1 ? () => (hidden.delete(item) || (hidden.size < all.length - 1 && !!hidden.add(item))) : null,
      })),
      tableView: () => table(
        [{ key: 'category', label: '날짜' }, ...all.map((item, k) => ({ key: `s${k}`, label: item.label, format }))],
        categories.map((category, i) => Object.fromEntries([['category', category], ...all.map((item, k) => [`s${k}`, item.values[i]])])),
      ),
    });
  }

  return { stackedBars, lines };
}
