import { decimal, endpoint, factors, finite, forecastEvidence, holdoutMetrics, integer, latestForecasts, lineGeometry, percent, transferRows } from './state.js';
import { stockName, factorInfo } from './opening-state.js';

const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const actions = { demo:'建合成資料', sync:'同步來源', research:'執行回測', holdout:'評估保留期',
  predict:'產生影子預測', settle:'結算結果' };
const statusNames = { queued:'等待', running:'執行中', succeeded:'成功', failed:'失敗' };
const providerNames = { fubon:'富邦', fugle:'富果', finmind:'FinMind', twse:'證交所',
  taifex:'期交所', tdcc:'集保', fred:'FRED', fmp:'FMP', gdelt:'GDELT', newsapi:'News API' };
const state = { config:null, workspace:null, view:'forecasts', rows:[], opened:new Set(),
  horizon:1, pending:false, operation:'', sequence:0 };
let observer;
let jobPoll;

async function read(name, options={}) {
  const response = await fetch(endpoint(state.config,name,state.workspace), {cache:'no-store',...options});
  if (!response.ok) throw new Error('讀取失敗');
  return response.json();
}

export function prepareDaily(config) {
  state.config = config;
  if (observer) return;
  observer = new IntersectionObserver(entries => {
    if (!entries.some(entry => entry.isIntersecting)) return;
    observer.disconnect();
    void connect();
  },{rootMargin:'120px'});
  observer.observe($('research'));
}

async function connect() {
  try {
    const listing = await read('workspaces');
    state.workspace = listing.default || state.config.default_workspace || 'demo';
    $('daily-workspace').innerHTML = listing.workspaces.map(item =>
      `<option value="${escape(item.id)}"${item.id===state.workspace ? ' selected' : ''}>${escape(item.name)}</option>`).join('');
    await refresh();
  } catch {
    $('daily-content').innerHTML = '<p class="empty">日線資料讀取失敗</p>';
  }
}

async function refresh() {
  if (!state.workspace) return;
  const sequence = ++state.sequence;
  clearTimeout(jobPoll);
  state.pending = true;
  render();
  const reads = { forecasts:['forecasts'], backtests:['status','equity'], sources:['sources','jobs','gaps'] };
  const names = reads[state.view];
  try {
    const data = await Promise.all(names.map(name => read(name)));
    if (sequence !== state.sequence) return;
    names.forEach((name,index) => { state[name] = data[index]; });
  } catch {
    if (sequence === state.sequence) state.operation = '資料讀取失敗';
  } finally {
    if (sequence === state.sequence) {
      state.pending = false;
      render();
      if (state.view === 'sources') watchJobs();
    }
  }
}

function equityCanvas() {
  const canvas = $('daily-equity');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const series = ['strategy_net','benchmark_buy_hold_net'];
  const data = lineGeometry(state.equity || [],series);
  if (!data) {
    canvas.hidden = true;
    return;
  }
  canvas.width=data.width; canvas.height=data.height;
  for (const [index,key] of series.entries()) {
    const path = data.paths[key];
    if (!path) continue;
    ctx.strokeStyle = index ? '#629786' : '#526df0';
    ctx.lineWidth = 2.5;
    ctx.stroke(new Path2D(path));
  }
}

const direction = row => finite(row.actual_return) ? percent(row.actual_return) : '待結算';
const amount = value => decimal(value,2);
const card = (key,value) => `<span><small>${escape(key)}</small><strong>${escape(value)}</strong></span>`;

function forecastTable() {
  const rows = latestForecasts(state.forecasts || [],state.horizon);
  if (!rows.length) return '<p class="empty">這個工作區尚無日線預測</p>';
  return `<div class="table-frame"><table><thead><tr><th>股票</th><th>基準日</th><th>收高機率</th><th>價格中心</th><th class="actual-col">到期報酬</th><th>細項</th></tr></thead><tbody>
    ${rows.map(row => {
      const open = state.opened.has(row.ticker);
      const id = `daily-${escape(row.ticker)}`;
      const label = forecastEvidence(row);
      return `<tr><td><b>${escape(row.ticker)}</b> ${escape(stockName(row.ticker))}</td>
        <td>${escape(row.anchor_date)}</td><td>${percent(row.probability_up,1)}</td><td>${amount(row.price_median)}</td>
        <td class="actual-col">${direction(row)}</td><td><button type="button" class="detail-toggle" aria-expanded="${open}" ${open ? `aria-controls="${id}"` : ''} data-daily-ticker="${escape(row.ticker)}">細項<i></i></button></td></tr>
        ${open ? `<tr class="detail-row" id="${id}"><td colspan="6">
          <div class="detail-head"><strong>${escape(label)}</strong><span>80% 經驗區間 ${amount(row.price_low)} – ${amount(row.price_high)}</span>
            <span>到期報酬 ${direction(row)}</span></div>
          <dl class="daily-factors">${factors(row.explanation).map(item => `<div><dt>${escape(factorInfo(item.feature)[0])}</dt><dd>${escape(percent(item.value))}</dd></div>`).join('')}</dl>
          <p class="detail-notice">因子分數為線性基準貢獻。</p>
          ${row.valuation?.scenarios?.length ? `<div class="daily-summary">${row.valuation.scenarios.map(item=>
            card({conservative:'保守估值',base:'基準估值',optimistic:'樂觀估值'}[item.name] || '估值',amount(item.fair_value))).join('')}</div>` : ''}
        </td></tr>` : ''}`;
    }).join('')}</tbody></table></div>`;
}

function backtestTable() {
  const report = state.status?.report;
  if (!report) return '<p class="empty">這個工作區尚無回測</p>';
  const metrics = report.metrics || {};
  const historical = metrics.by_horizon?.[state.horizon] || {};
  const final = holdoutMetrics(report)?.by_horizon?.[state.horizon] || {};
  const transfers = transferRows(report,state.horizon,'oos');
  const portfolio = metrics.portfolio || {};
  return `<div class="daily-summary">${card('方向準確率',percent(historical.accuracy))}
    ${card('保留期準確率',percent(final.accuracy))}${card('回測觀測',integer(historical.observations))}
    ${card('Brier／基準',`${decimal(historical.brier,3)} / ${decimal(historical.baseline_brier,3)}`)}</div>
    <div class="daily-summary">${card('1 日策略累積報酬',percent(portfolio.strategy?.total_return))}
      ${card('0050 同期持有',percent(portfolio.benchmark_buy_hold?.total_return))}
      ${card('最大回撤',percent(portfolio.strategy?.max_drawdown))}</div>
    ${state.equity?.length ? '<canvas id="daily-equity" aria-label="同期策略與0050持有淨值" role="img"></canvas>' : ''}
    ${transfers.length ? `<h3 class="daily-subtitle">延伸標的</h3><div class="table-frame"><table><thead><tr><th>標的</th><th>方向準確率</th><th>觀測</th></tr></thead>
    <tbody>${transfers.map(row=>`<tr><td>${escape(row.ticker)} ${escape(stockName(row.ticker))}</td><td>${percent(row.accuracy)}</td><td>${integer(row.observations)}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
}

function sourceTable() {
  const operations = state.jobs?.jobs || [];
  const busy = operations.some(row => ['queued','running'].includes(row.status));
  const sources = state.sources?.sources || [];
  return `<div class="daily-source-list">${sources.map(row => `<span>${escape(providerNames[row.provider] || row.provider)}
    <small>${escape(({configured_unverified:'已設定', public_unverified:'公開', missing_credentials:'待設定',ok:'已驗證'})[row.status] || '待驗證')}</small></span>`).join('')}</div>
    <div class="daily-actions">${state.config.capabilities?.actions
      ? Object.entries(actions).map(([key,label])=>`<button type="button" class="secondary" data-daily-action="${key}"${busy ? ' disabled' : ''}>${label}</button>`).join('')
      : '<small>合成預覽 · 唯讀</small>'}</div>
    ${state.operation ? `<p class="daily-message">${escape(state.operation)}</p>` : ''}
    ${operations.length ? `<ul class="job-list">${operations.slice(0,4).map(item=>
      `<li><b>${escape(actions[item.action] || item.action)}</b><span>${escape(statusNames[item.status] || item.status)}</span></li>`).join('')}</ul>` : ''}
    ${state.gaps?.items?.length ? `<details class="daily-gaps"><summary>資料與驗證缺口</summary><ul>${state.gaps.items.map(item=>
      `<li>${escape(item.title)} <small>${escape(item.priority)}</small></li>`).join('')}</ul></details>` : ''}`;
}

function render() {
  for (const button of $('research').querySelectorAll('[data-daily-view]')) {
    button.setAttribute('aria-pressed', String(state.view === button.dataset.dailyView));
  }
  if (state.pending) {
    $('daily-content').innerHTML = '<p class="empty">讀取中…</p>';
    return;
  }
  $('daily-content').innerHTML = ({forecasts:forecastTable,backtests:backtestTable,sources:sourceTable})[state.view]();
  equityCanvas();
}

async function submit(key) {
  if (!Object.hasOwn(actions,key) || !state.config.capabilities?.actions) return;
  try {
    const session = await read('session');
    await read('jobs',{method:'POST',headers:{'Content-Type':'application/json','X-TWQuant-Token':session.action_token},
                       body:JSON.stringify({action:key,workspace:state.workspace})});
    state.operation = '已排入本機工作';
  } catch {
    state.operation = '工作未啟動';
  }
  await refresh();
}

function watchJobs() {
  if (state.config.mode !== 'local' || !(state.jobs?.jobs || []).some(item => ['queued','running'].includes(item.status))) return;
  const sequence = state.sequence;
  jobPoll = setTimeout(async () => {
    try {
      const jobs = await read('jobs');
      if (sequence !== state.sequence || state.view !== 'sources') return;
      state.jobs = jobs;
      render();
      watchJobs();
    } catch {
      if (sequence === state.sequence) state.operation = '工作狀態暫時無法讀取';
    }
  },1500);
}

$('research').addEventListener('click', event => {
  const node = event.target.closest('[data-daily-view],[data-daily-ticker],[data-daily-action]');
  if (!node) return;
  if (node.dataset.dailyView) {
    state.view = node.dataset.dailyView;
    state.operation = '';
    void refresh();
  } else if (node.dataset.dailyTicker) {
    const key = node.dataset.dailyTicker;
    if (state.opened.has(key)) state.opened.delete(key);
    else state.opened.add(key);
    render();
    [...$('daily-content').querySelectorAll('[data-daily-ticker]')].find(item => item.dataset.dailyTicker === key)?.focus({preventScroll:true});
  } else {
    void submit(node.dataset.dailyAction);
  }
});
$('daily-workspace').addEventListener('change', event => {
  state.workspace = event.target.value;
  for (const key of ['forecasts','status','equity','sources','jobs','gaps']) delete state[key];
  state.opened.clear();
  void refresh();
});
$('daily-horizon').addEventListener('change', event => {
  state.horizon = Number(event.target.value);
  state.opened.clear();
  render();
});
