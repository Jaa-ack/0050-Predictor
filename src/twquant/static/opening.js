import { finite, integer, percent } from './state.js';
import { factorInfo, groupNames, openingRows, stockName } from './opening-state.js';
import { renderFlow } from './flow.js';
import { prepareDaily } from './daily.js';

const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
})[char]);
const number = new Intl.NumberFormat('zh-TW', { maximumFractionDigits:3 });
const time = new Intl.DateTimeFormat('zh-TW', { timeZone:'Asia/Taipei', hourCycle:'h23',
  hour:'2-digit', minute:'2-digit', second:'2-digit', fractionalSecondDigits:3 });
const fixed = ['2330','2454','2317','2308','3711','1303','2383','2881','2891','3037'];
const modelNames = { auction_logistic:'原試撮模型', auction_repair:'試撮＋修正' };
const fallbackText = { trial_missing:'截止前試撮缺報 · 使用訓練期比例',
  model_unavailable:'訓練樣本不足 · 使用訓練期比例', trial_missing_not_up:'截止前試撮缺報 · 預測未高於',
  repair_unavailable_trial:'修正樣本不足 · 採用試撮方向' };
const pageSize = 10;
const app = { view:'week', day:null, page:0, expanded:new Set(), rows:[], request:0, loading:false };

async function load(url) {
  const response = await fetch(url, { cache:'no-store' });
  if (!response.ok) throw new Error('資料讀取失敗');
  return response.json();
}

function evidence() {
  const kind = app.data?.[app.view]?.evidence;
  if (app.config?.mode === 'snapshot' || kind === 'synthetic') return '合成示範';
  return kind ? '實際收件重播' : '等待試撮';
}

function metric(title, value, detail) {
  return `<div class="metric"><span>${escape(title)}</span><strong>${escape(value)}</strong><small>${escape(detail || '　')}</small></div>`;
}

function renderComparison(pair) {
  $('comparison-date').textContent = app.day ? `${app.day} · ${evidence()}` : '等待驗證';
  if (!pair) {
    $('comparison').innerHTML = '<p class="empty">等待這一天的同樣本比較</p>';
    return;
  }
  const entries = [
    ['原試撮模型', pair.original, pair.original_paired, app.view === 'week'],
    ['直接試撮', pair.trial, null, false],
    ['試撮＋修正', pair.repair, pair.repair_paired, app.view === 'repair']
  ];
  $('comparison').innerHTML = `<table><thead><tr><th scope="col">方案</th>
    <th scope="col">命中數</th><th scope="col">準確率</th><th scope="col">最弱五檔</th>
    <th scope="col">上漲召回</th><th scope="col">未上漲召回</th><th scope="col">相對試撮 救回／新增錯誤</th>
    </tr></thead><tbody>${entries.map(([label, score, diff, active]) => `
    <tr class="${active ? 'active-model' : ''}"><th scope="row">${label}</th>
    <td>${integer(score.correct)}／${integer(score.expected)}</td><td><strong>${percent(score.accuracy,1)}</strong></td>
    <td>${percent(score.worst_five,1)}</td><td>${percent(score.recall?.up,1)}</td><td>${percent(score.recall?.not_up,1)}</td>
    <td>${diff ? `${integer(diff.rescued)}／${integer(diff.added_errors)}` : '基準'}</td></tr>`).join('')}</tbody></table>`;
}

function renderOverview() {
  const { week, target } = app.data;
  const active = app.data[app.view];
  const pair = app.data.comparisons?.[app.day];
  const isRepair = app.view === 'repair';
  const selectedScore = isRepair ? pair?.repair : pair?.original;
  const selectedRows = isRepair ? pair?.repair_training_rows : pair?.original_training_rows;
  const fallback = active.date === app.day;
  const accuracy = selectedScore?.accuracy ?? (fallback ? active.accuracy : null);
  const diagnostics = (isRepair ? pair?.repair_diagnostics : pair?.original_diagnostics)
    || (fallback ? active.diagnostics : {});
  const captured = week.days.filter(day => finite(day.inputs));
  const receiptCount = captured.reduce((count, day) => count + day.inputs, 0);
  const receiptTotal = captured.reduce((count, day) => count + day.expected, 0);
  const countDisplay = captured.length ? `${integer(receiptCount)} / ${integer(receiptTotal)}` : '—';
  const caption = week.days.length ? `${captured.length} / ${week.days.length} 個收件日` : '等待排程';
  $('mode').textContent = evidence();
  $('rail-status').textContent = app.config.mode === 'snapshot' ? '唯讀合成快照' : '本機研究資料';
  for (const tab of document.querySelectorAll('[data-view]')) {
    tab.setAttribute('aria-pressed', String(tab.dataset.view === app.view));
  }

  const trainRows = selectedRows ?? (fallback ? active.training_rows : null);
  const correction = pair?.correction ?? (fallback ? active.correction : null);
  $('signal-column').textContent = isRepair ? '修正後價差' : '高於前收分數';
  $('metrics').innerHTML = [
    metric(isRepair ? '修正模型準確率' : '原模型準確率', percent(accuracy, 1),
      finite(accuracy) ? `${app.day} · 單日留出` : '等待該日驗證'),
    metric('試撮方向基準', percent(pair?.trial?.accuracy ?? (fallback ? active.trial_rule : null), 1),
      `完整 50 檔 · 研究目標 ${percent(target, 0)}`),
    metric('固定十檔訓練', finite(trainRows) ? `${integer(trainRows)} 筆` : '—',
      isRepair && finite(correction) ? `共同修正 ${basisPoints(correction)}` : '留出日以前的成熟樣本'),
    metric('即時試撮覆蓋', countDisplay, caption),
  ].join('');
  renderComparison(pair);
  renderFlow({ view:app.view, day:app.day, accuracy, diagnostics, trainRows, correction,
    fitted:isRepair ? (pair || fallback ? finite(correction) : null) :
      pair?.original_fitted ?? (fallback ? active.fitted : null),
    captured:captured.length, days:week.days.length });
  renderTrend();
  $('decision-day').innerHTML = active.days.length ? [...active.days].reverse().map(day =>
    `<option value="${escape(day.date)}"${day.date === app.day ? ' selected' : ''}>${escape(day.date)}</option>`).join('')
    : '<option value="">尚無交易日</option>';
  $('decision-day').disabled = !active.days.length;
}

function renderTrend() {
  const days = app.data.week.days;
  const selectedIndex = days.findIndex(item => item.date === app.day);
  const end = selectedIndex < 0 ? days.length : Math.max(12, selectedIndex + 1);
  const series = days.slice(Math.max(0, end - 12), end);
  $('trend-title').textContent = '逐日試撮覆蓋';
  $('trend-subtitle').textContent = series.length ?
    `${series[0].date} – ${series.at(-1).date}` : '等待研究快照';
  $('trend-legend').textContent = '截止前有效筆數';
  if (!series.length) {
    $('trend').innerHTML = '<p class="empty">尚無可用驗證結果</p>';
    return;
  }
  const ticks = [0,25,50,75,100];
  const grid = ticks.map(tick => `<i class="gridline" style="bottom:${tick}%"></i>`).join('');
  const selected = series.find(item => item.date === app.day);
  const caption = !selected ? '' :
    `${selected.date} · ${finite(selected.inputs) ? `${integer(selected.inputs)} / ${integer(selected.expected)} 筆` : '等待收件'}`;
  const bars = series.map((day, i) => {
    const value = finite(day.inputs) && day.expected ? day.inputs / day.expected : null;
    const label = `${day.date} ${finite(value) ? percent(value, 1) : '等待收件'}`;
    return `<div class="bar-item"><button type="button" data-day="${escape(day.date)}"
      class="${finite(value) ? '' : 'no-data'}" aria-label="${escape(label)}" title="${escape(label)}"
      aria-pressed="${day.date === app.day}" style="--value:${safeWidth(value)}%"></button>
      ${series.length <= 6 || i % 3 === 0 || day.date === app.day
      ? `<small aria-hidden="true">${escape(day.date.slice(5).replace('-','/'))}</small>` : ''}</div>`;
  }).join('');
  $('trend').innerHTML = `<div class="chart"><div class="scale" aria-hidden="true">${ticks.map(tick =>
    `<span style="bottom:${tick}%">${tick}%</span>`).join('')}</div>
    <div class="plot" style="--bars:${series.length}">${grid}${bars}</div></div>
    <div class="chart-caption">${escape(caption || '選擇日期查看細項')}</div>`;
}

const safeWidth = value => finite(value) ? Math.max(0, Math.min(100, Number(value) * 100)) : 0;
const direction = value => value == null ? '—' :
  `<span class="direction ${value ? 'up' : 'down'}"><i aria-hidden="true"></i>${value ? '高於' : '未高於'}</span>`;
const outcome = row => row.correct === true ? ['hit','命中'] : row.correct === false ? ['miss','未命中'] : ['','缺答案'];
const showTime = value => finite(value) ? time.format(new Date(value / 1000)) : '—';
const points = value => finite(value) ? `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)} 點` : '—';
const basisPoints = value => finite(value) ? `${value > 0 ? '+' : ''}${(value * 10000).toFixed(1)} bp` : '—';
const impactTone = value => value > 0 ? 'up' : value < 0 ? 'down' : '';

function factorValue(key, value) {
  if (!finite(value)) return '缺資料';
  const unit = factorInfo(key)[1];
  if (unit === 'percent') return percent(value, 2);
  if (unit === 'ratio') return `${number.format(value)} 倍`;
  if (unit === 'rate') return `${number.format(value)}%`;
  if (unit === 'log-size') return `${number.format(Math.expm1(value))} 張`;
  return number.format(value);
}

function detail(row, id) {
  const groups = [...(row.groups || [])].sort((a,b) => Math.abs(b.impact || 0) - Math.abs(a.impact || 0));
  const maximum = Math.max(...groups.map(group => Math.abs(group.impact || 0)), .001);
  const status = outcome(row)[1];
  const factors = groups.map((group, index) => {
    const value = group.impact || 0;
    const tone = impactTone(value);
    return `<details class="factor-group"${index === 0 ? ' open' : ''}><summary>
      <span>${escape(groupNames[group.group] || group.group)}</span>
      <span class="impactbar" aria-hidden="true"><i class="${tone}" style="width:${Math.abs(value) / maximum * 49}%"></i></span>
      <b class="impactvalue ${tone}">${points(group.impact)}</b></summary>
      <dl class="factor-values">${group.features.map(feature => {
        const label = factorInfo(feature.key)[0];
        const observed = factorValue(feature.key, feature.value);
        return `<div><dt>${escape(label)}</dt><dd>${escape(observed)}${feature.imputed ? ' · 已填補' : ''}
          ${finite(feature.impact) ? ` · ${escape(points(feature.impact))}` : ''}</dd></div>`;
      }).join('')}</dl></details>`;
  }).join('');
  const model = modelNames[row.model] || '';
  const repair = row.model === 'auction_repair';
  return `<tr class="detail-row" id="${id}"><td colspan="6">
    <div class="detail-head"><strong>影響因素</strong>${model ? `<span>${escape(model)}</span>` : ''}
    ${repair ? '' : `<span>基準分數 ${percent(row.factor_base, 1)}</span>`}
    <span>正式開盤 ${direction(row.actual_up)} · ${status}</span>
    ${finite(row.receipt_us) ? `<span>交易所 ${showTime(row.exchange_us)} · 收件 ${showTime(row.receipt_us)}</span>` : ''}</div>
    ${row.fallback ? `<p class="detail-notice">${escape(fallbackText[row.fallback] || '使用模型基準')}</p>` : ''}
    ${repair ? `<dl class="repair-detail">
      <div><dt>試撮價差</dt><dd>${basisPoints(row.trial_gap)}</dd></div>
      <div><dt>固定十檔中位數修正</dt><dd>${basisPoints(row.correction)}</dd></div>
      <div><dt>修正後價差</dt><dd>${basisPoints(row.adjusted_gap)}</dd></div>
      </dl>` : groups.length ? `<div class="factor-grid">${factors}</div>
      <p class="detail-notice">按輸入順序加入因子的分數變化，單位為百分點。</p>`
      : !row.fallback ? '<p class="detail-notice">這個版本沒有保存逐筆因子。</p>' : ''}
    </td></tr>`;
}

function renderRows() {
  const rows = openingRows(app.rows, $('query').value, $('result-filter').value);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  app.page = Math.min(app.page, pages - 1);
  const first = app.page * pageSize;
  $('rows').innerHTML = rows.slice(first, first + pageSize).map((row, i) => {
    const open = app.expanded.has(row.ticker);
    const [status, label] = outcome(row);
    const detailId = `detail-${i}`;
    return `<tr class="observation">
      <td><span class="company"><span class="ticker">${escape(row.ticker)}</span>
        <span class="company-name">${escape(stockName(row.ticker))}</span></span></td>
      <td>${direction(row.predicted_up)}</td>
      <td><div class="probability">${app.view === 'repair' ? basisPoints(row.adjusted_gap) : percent(row.probability, 1)}
        ${finite(row.probability) ? `<span class="probability-track" aria-hidden="true"><i style="width:${safeWidth(row.probability)}%"></i></span>` : ''}</div></td>
      <td class="actual-col">${direction(row.actual_up)}</td>
      <td class="result-col"><span class="outcome ${status}">${label}</span></td>
      <td><button type="button" class="detail-toggle" data-ticker="${escape(row.ticker)}"
        aria-label="${escape(row.ticker)} 影響因素" aria-expanded="${open}"${open ? ` aria-controls="${detailId}"` : ''}>影響因素<i aria-hidden="true"></i></button></td>
      </tr>${open ? detail(row, detailId) : ''}`;
  }).join('');
  $('table-empty').hidden = rows.length > 0;
  $('table-empty').textContent = app.loading ? '載入逐筆結果…' : app.rowError ? '逐筆結果讀取失敗' :
    app.rows.length ? '沒有符合條件的預測' : '等待該日驗證結果';
  $('row-count').textContent = rows.length ? `第 ${first + 1}–${Math.min(first + pageSize,rows.length)} 筆，共 ${rows.length} 筆` : '0 筆';
  $('page-number').textContent = `${app.page + 1} / ${pages}`;
  $('previous').disabled = app.page < 1;
  $('next').disabled = app.page >= pages - 1;
}

function rank(ticker) {
  const order = fixed.indexOf(ticker);
  return order === -1 ? fixed.length : order;
}

async function select(view, day, chartFocus = false) {
  const source = app.data[view];
  const request = ++app.request;
  app.view = view;
  app.day = (source.days.some(item => item.date === day) ? day : null)
    || source.date || source.days.at(-1)?.date || null;
  app.rows = [];
  app.expanded.clear();
  app.page = 0;
  app.rowError = false;
  app.loading = true;
  $('prediction-context').textContent = `${app.day || '待驗證'} · ${evidence()}`;
  renderOverview();
  renderRows();
  if (chartFocus) Array.from($('trend').querySelectorAll('[data-day]')).find(button =>
    button.dataset.day === app.day)?.focus({preventScroll:true});
  try {
    const params = new URLSearchParams({ view });
    if (app.day) params.set('date', app.day);
    const result = app.config.mode === 'snapshot' ? { rows: app.data.rows?.[view]?.[app.day] || [] }
      : await load(`./api/opening/predictions?${params}`);
    if (request === app.request) app.rows = [...result.rows].sort((a,b) =>
      rank(a.ticker) - rank(b.ticker) || String(a.ticker).localeCompare(String(b.ticker)));
  } catch {
    if (request === app.request) app.rowError = true;
  } finally {
    if (request === app.request) {
      app.loading = false;
      renderRows();
    }
  }
}

async function refresh() {
  $('refresh').disabled = true;
  $('alert').hidden = true;
  try {
    app.config = await load('./app-config.json');
    prepareDaily(app.config);
    app.data = await load(app.config.mode === 'snapshot' ? './data/opening.json' : './api/opening');
    await select(app.view, app.day);
  } catch {
    $('alert').hidden = false;
    $('alert').textContent = '研究資料讀取失敗。';
  } finally {
    $('refresh').disabled = false;
  }
}

for (const button of document.querySelectorAll('[data-view]')) {
  button.addEventListener('click', () => { if (app.data) void select(button.dataset.view, app.day); });
}
for (const link of document.querySelectorAll('.nav .nav-link')) {
  link.addEventListener('click', () => {
    for (const item of document.querySelectorAll('.nav .nav-link')) item.classList.toggle('active', item === link);
  });
}
$('trend').addEventListener('click', event => {
  const button = event.target.closest('[data-day]');
  if (button) void select(app.view, button.dataset.day, true);
});
$('rows').addEventListener('click', event => {
  const button = event.target.closest('[data-ticker]');
  if (!button) return;
  const ticker = button.dataset.ticker;
  if (app.expanded.has(ticker)) app.expanded.delete(ticker);
  else app.expanded.add(ticker);
  renderRows();
  Array.from($('rows').querySelectorAll('[data-ticker]')).find(item =>
    item.dataset.ticker === ticker)?.focus({preventScroll:true});
});
$('decision-day').addEventListener('change', event => { if (app.data) void select(app.view, event.target.value); });
$('query').addEventListener('input', () => { app.page = 0; renderRows(); });
$('result-filter').addEventListener('change', () => { app.page = 0; renderRows(); });
$('previous').addEventListener('click', () => { app.page--; renderRows(); });
$('next').addEventListener('click', () => { app.page++; renderRows(); });
$('refresh').addEventListener('click', () => void refresh());
void refresh();
