import { finite, integer, percent } from './state.js';
import { factorInfo } from './opening-state.js';
import { tracedEdges } from './flow-state.js';

const $ = id => document.getElementById(id);
const safe = value => String(value ?? '').replace(/[&<>"']/g, key => (
  {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[key]));
const auctionSources = {
  trial_gap:{label:'試撮相對前收', detail:'最後一筆合格試撮價相對上一交易日收盤的變動。'},
  log_matched_size:{label:'試撮成交量', detail:'最後一筆合格試撮量，以 log(1＋量) 入模。'},
  book_imbalance:{label:'五檔委買賣', detail:'五檔買量減賣量，再除以買賣總量。'}
};
let selection = 'trial_gap';
let state = null;
let pendingFrame;
let curves = [];

function modelNodes(data) {
  const repair = data.view === 'repair';
  const fitted = data.fitted;
  const ready = finite(data.accuracy);
  const train = finite(data.trainRows) ? `${integer(data.trainRows)} 筆` : '—';
  return {
    cutoff:{label:'08:59 雙時鐘', detail:'比對交易所發生時間與 VM 實際收件時間', fields:[
      ['試撮窗口','08:58:30 至 08:59 前'], ['已收件',`${data.captured} / ${data.days} 日`]]},
    model:{label:repair ? '中位數修正' : '邏輯斯迴歸',
      detail:repair ? '試撮價差加固定十檔的共同修正' : '用同一組試撮因子預測 50 檔',
      badge:fitted === false ? '回退' : fitted ? '已擬合' : '等待', fields:[
        ['訓練範圍','2330、2454、2317、2308、3711、1303、2383、2881、2891、3037'],
        ['成熟訓練樣本',train],
        [repair ? '共同修正' : '上漲門檻', repair ? finite(data.correction) ?
          `${signed(data.correction * 10000, 1)} bp` : '沿用試撮方向' : '分數大於 50%']]},
    result:{label:'高於前收？', detail:'08:59 判斷當日正式開盤是否高於上一交易日收盤',
      fields:[['高於',repair ? '修正後價差 > 0' : '模型分數 > 50%'], ['未高於','平盤或低於前收']]},
    audit:{label:'全成分驗證', detail:'完整 50 檔留出驗證，缺答計入分母',
      badge:ready ? '可查' : '等待', fields:[['驗證日',data.day || '—'],
        ['整體準確率',percent(data.accuracy,1)]]}
  };
}

function signed(value, digits = 2) {
  return finite(value) ? `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(digits)}` : '—';
}

function inspect(id) {
  const active = state.sources[id];
  const stage = state.nodes[id];
  if (!active && !stage) return;
  const data = state.stats[id];
  const panel = $('flow-inspector');
  if (stage) {
    panel.innerHTML = `<div class="flow-caption">系統節點</div><h3>${safe(stage.label)}</h3><p>${safe(stage.detail)}</p>
      <dl class="flow-facts">${stage.fields.map(([key,value])=>`<div><dt>${safe(key)}</dt><dd>${safe(value)}</dd></div>`).join('')}</dl>`;
    return;
  }
  const measured = data?.features || [];
  const weights = measured.some(item => finite(item.coefficient));
  panel.innerHTML = `<div class="flow-caption">模型輸入</div><h3>${safe(active.label)}</h3><p>${safe(active.detail)}</p>
    <div class="flow-metrics">
      <div><span>有效覆蓋</span><strong>${percent(data?.coverage,1)}</strong></div>
      ${state.view === 'repair' ? '' : `
      <div><span>平均絕對影響</span><strong>${finite(data?.impact) ? `${(data.impact*100).toFixed(2)} 點` : '—'}</strong></div>
      <div><span>影響比重</span><strong>${percent(data?.impact_share,1)}</strong></div>`}
    </div>
    ${measured.length ? `<div class="flow-factors" role="region" aria-label="來源因子明細" tabindex="0">
      <table><thead><tr><th>因子</th><th>相關 r</th><th>樣本</th>${weights ? '<th>係數</th>' : ''}</tr></thead>
        <tbody>${measured.map(item=>`<tr><td>${safe(factorInfo(item.key)[0])}</td><td>${signed(item.correlation)}</td>
          <td>${integer(item.samples)}</td>${weights ? `<td>${signed(item.coefficient,3)}</td>` : ''}</tr>`).join('')}</tbody></table></div>
      <p class="flow-note">相關 r 以該驗證範圍的實際開盤方向計算。${weights ? '係數使用標準化輸入。' : ''}</p>`
    : '<p class="flow-note">待驗證後顯示這一天的數值。</p>'}`;
}

function show(id) {
  if (selection === id && $('flow-inspector').hasChildNodes()) return;
  selection = id;
  update();
}

function update() {
  inspect(selection);
  for (const button of $('flow-board').querySelectorAll('[data-node]')) {
    button.setAttribute('aria-pressed', String(button.dataset.node === selection));
  }
  drawLater();
}

function drawLater() {
  cancelAnimationFrame(pendingFrame);
  pendingFrame = requestAnimationFrame(draw);
}

function draw() {
  const panel = $('flow-board');
  if (!state || !panel.clientWidth) return;
  const canvas = $('flow-canvas');
  const box = panel.getBoundingClientRect();
  const scale = Math.min(2, devicePixelRatio || 1);
  canvas.width = box.width * scale;
  canvas.height = box.height * scale;
  const context = canvas.getContext('2d');
  context.scale(scale,scale);
  context.lineCap = 'round';
  const active = new Set(tracedEdges(state.links, selection));
  curves = [];

  for (const edge of [...state.links].sort((a,b)=>Number(active.has(a))-Number(active.has(b)))) {
    const from = panel.querySelector(`[data-node="${edge.from}"]`).getBoundingClientRect();
    const to = panel.querySelector(`[data-node="${edge.to}"]`).getBoundingClientRect();
    const x0 = from.right - box.left;
    const x1 = to.left - box.left;
    const y0 = (from.top + from.bottom)/2 - box.top;
    const y1 = (to.top + to.bottom)/2 - box.top;
    const middle = (x0+x1)/2;
    const info = state.stats[edge.from];
    const weight = finite(info?.impact_share) ? Number(info.impact_share) : 0;
    const missing = state.sources[edge.from] && !finite(info?.coverage);
    const current = active.has(edge);
    const gradient = context.createLinearGradient(x0,y0,x1,y1);
    gradient.addColorStop(0,current ? '#86E7E4' : '#4f6092');
    gradient.addColorStop(1,current ? '#87A4FF' : '#40557f');
    context.beginPath();
    context.strokeStyle = gradient;
    context.globalAlpha = current ? .96 : .55;
    context.lineWidth = 1.25 + weight * 9 + (current ? .8 : 0);
    context.setLineDash(missing ? [4,5] : []);
    context.moveTo(x0,y0);
    context.bezierCurveTo(middle,y0,middle,y1,x1,y1);
    context.stroke();
    curves.push({edge, x0,x1,y0,y1,middle});
  }
}

export function renderFlow(data) {
  const repair = data.view === 'repair';
  const { auction, ...stats } = data.diagnostics || {};
  const keys = repair ? ['trial_gap'] : data.fitted === false ? [] : auction ?
    auction.features.map(item => item.key).filter(key => auctionSources[key]) : Object.keys(auctionSources);
  const sources = Object.fromEntries(keys.map(key => [key, auctionSources[key]]));
  const stages = [
    ...(keys.length ? [['資料輸入', keys], ['時點檢查', ['cutoff']]] : []),
    ['模型', ['model']], ['結果', ['result','audit']]
  ];
  const links = [
    ...keys.map(from => ({from, to:'cutoff'})),
    ...(keys.length ? [{from:'cutoff',to:'model'}] : []),
    {from:'model',to:'result'}, {from:'model',to:'audit'}
  ];
  state = { view:data.view, stats, sources, links, nodes:modelNodes(data) };
  if (!sources[selection] && !stages.some(([,ids]) => ids.includes(selection))) selection = keys[0] || 'model';
  $('flow-key').textContent = repair ? '價差修正 · bp' : '線寬表示影響比重';
  $('flow-layer').dataset.columns = String(stages.length);
  $('flow-layer').innerHTML = stages.map(([title, ids]) =>
    `<div class="flow-column"><div class="flow-layer-name">${title}</div><div class="flow-nodes">
      ${ids.map(id => {
        const node = sources[id] || state.nodes[id];
        const stat = state.stats[id];
        const badge = sources[id] ? finite(stat?.impact_share) ? percent(stat.impact_share,0) : '' : node.badge;
        return `<button type="button" data-node="${id}" class="flow-node ${sources[id] ? 'source-node' : ''}"
          aria-controls="flow-inspector" title="${safe(node.detail)}"><span>${safe(node.label)}</span>${badge ? `<small>${safe(badge)}</small>` : ''}</button>`;
      }).join('')}</div></div>`).join('');
  update();
}

const board = $('flow-board');
new ResizeObserver(drawLater).observe(board);
for (const event of ['pointerover', 'focusin', 'click']) {
  board.addEventListener(event, interaction => {
    const button = interaction.target.closest('[data-node]');
    if (button) {
      show(button.dataset.node);
      if (event === 'click' && interaction.pointerType === 'touch') {
        $('flow-inspector').scrollIntoView({block:'nearest'});
      }
    }
  });
}
board.addEventListener('pointermove', event => {
  if (event.pointerType !== 'mouse' || event.target.closest('[data-node]')) return;
  const bounds = board.getBoundingClientRect();
  const x = event.clientX - bounds.left;
  const y = event.clientY - bounds.top;
  let closest = null;
  let minimum = 8;
  for (const curve of curves) {
    if (x < curve.x0 || x > curve.x1) continue;
    for (let i=0; i<=16; i++) {
      const t = i/16;
      const u = 1-t;
      const cx = u*u*u*curve.x0 + 3*u*t*curve.middle + t*t*t*curve.x1;
      const cy = (u*u*u+3*u*u*t)*curve.y0 + (3*u*t*t+t*t*t)*curve.y1;
      const distance = Math.hypot(x-cx,y-cy);
      if (distance < minimum) { closest = curve.edge; minimum = distance; }
    }
  }
  if (closest) show(closest.from);
});
