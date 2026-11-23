import { finite, integer, percent } from './state.js';
import { groupNames, factorInfo } from './opening-state.js';
import { tracedEdges } from './flow-state.js';

const $ = id => document.getElementById(id);
const safe = value => String(value ?? '').replace(/[&<>"']/g, key => (
  {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[key]));
const sources = {
  price:{label:'台股價格', detail:'報酬、均線、波動、同儕表現'},
  volume:{label:'成交量', detail:'相對成交量、流動性'},
  us:{label:'美股與 ADR', detail:'大盤、台積電 ADR、個股與產業隔夜報酬'},
  night:{label:'期貨夜盤', detail:'臺指期、電子期、金融期的報酬、振幅與量比'},
  other:{label:'籌碼與總經', detail:'法人買賣超、融資券、VIX、匯率、美債殖利率'},
  auction:{label:'盤前試撮', detail:'試撮價相對前收、試撮量、五檔委買賣'},
  news:{label:'公告與新聞', detail:'公告及媒體事件數、情緒分數'},
  morning:{label:'早盤期貨與匯率', detail:'08:45 起的臺指期與美元兌臺幣變動'}
};
const stages = [
  ['資料輸入', Object.keys(sources)],
  ['時點檢查', ['snapshot', 'cutoff']],
  ['模型', ['model']],
  ['結果', ['result','audit']]
];
const links = [
  ...Object.keys(sources).map(from => ({from, to:['auction','news','morning'].includes(from) ? 'cutoff' : 'snapshot'})),
  {from:'snapshot',to:'model'}, {from:'cutoff',to:'model'},
  {from:'model',to:'result'}, {from:'model',to:'audit'}
];
let selection = 'night';
let state = null;
let pendingFrame;
let curves = [];

function modelNodes(data, view) {
  const week = view === 'week';
  const captured = data.week.days.filter(day => finite(day.inputs)).length;
  const ready = data[view].status === 'ready';
  const sessions = data.history.sessions;
  const historyRange = finite(sessions) ? `${integer(sessions)} 個${data.history.evidence === 'synthetic' ? '示範' : '歷史'}交易日` : '—';
  return {
    snapshot:{label:'08:30 快照', detail:'上一交易日歷史價量與盤前跨市場資料', badge:'決策前', fields:[
      ['輸入','價量、美股、夜盤與籌碼'], ['檢查','只使用當時已可得的資料']]},
    cutoff:{label:'08:59 雙時鐘', detail:'核對交易所發生時間與 VM 實際收件時間', badge:'截止', fields:[
      ['試撮窗口','08:58:30 – 08:59 前'], ['已收件',`${captured} / ${data.week.days.length} 日`]]},
    model:{label:'固定十檔', detail:week ? '前四天擬合試撮邏輯斯模型' : '按時間分折，在固定十檔校準後選模',
      badge:ready ? '可查' : '等待', fields:[
        ['訓練範圍','2330、2454、2317、2308、3711、1303、2383、2881、2891、3037'],
        ['模型',week ? '試撮邏輯斯迴歸' : '邏輯斯迴歸與梯度提升樹']]},
    result:{label:'高於前收？', detail:'08:59 判斷當日正式開盤是否高於上一交易日收盤',
      badge:'二元', fields:[['高於','正式開盤價 > 前收'], ['未高於','平盤或低於前收']]},
    audit:{label:'全成分驗證', detail:'同一模型覆蓋全部 50 檔，保留缺答並報告最弱五檔',
      badge:ready ? '可查' : '等待', fields:[['驗證範圍',week ? '11/27 完整 50 檔' : historyRange],
        ['目標',percent(data.target,0)], ['整體準確率',percent(data[view].accuracy,2)]]}
  };
}

function signed(value, digits = 2) {
  return finite(value) ? `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(digits)}` : '—';
}

function inspect(id) {
  let active = sources[id];
  const stage = state.nodes[id];
  if (!active && !stage) return;
  if (id === 'auction' && state.view === 'week') {
    active = { ...active, detail:'試撮相對前收、試撮量、五檔不平衡。08:59 前收件才入模。' };
  }
  const data = state.stats[id];
  const panel = $('flow-inspector');
  if (stage) {
    panel.innerHTML = `<div class="flow-caption">系統節點</div><h3>${safe(stage.label)}</h3><p>${safe(stage.detail)}</p>
      <dl class="flow-facts">${stage.fields.map(([key,value])=>`<div><dt>${safe(key)}</dt><dd>${safe(value)}</dd></div>`).join('')}</dl>`;
    return;
  }
  const measured = data?.features || [];
  const historical = state.historicalInputs[id];
  if (historical?.length && !measured.length) {
    const names = historical.map(key => safe(factorInfo(key)[0])).join('、');
    panel.innerHTML = `<div class="flow-caption">真實歷史模型已使用</div><h3>${safe(active.label)}</h3>
      <dl class="flow-facts"><div><dt>採用因子</dt><dd>${names}</dd></div></dl>
      <p class="flow-note">此處是合成示範。實測覆蓋率、相關性和模型影響可在本機歷史驗證查看。</p>`;
    return;
  }
  const weights = measured.some(item => finite(item.coefficient));
  panel.innerHTML = `<div class="flow-caption">資料來源</div><h3>${safe(groupNames[id] || active.label)}</h3><p>${safe(active.detail)}</p>
    <div class="flow-metrics">
      <div><span>有效覆蓋</span><strong>${percent(data?.coverage,1)}</strong></div>
      <div><span>平均絕對影響</span><strong>${finite(data?.impact) ? `${(data.impact*100).toFixed(2)} 點` : '—'}</strong></div>
      <div><span>影響比重</span><strong>${percent(data?.impact_share,1)}</strong></div>
    </div>
    ${measured.length ? `<div class="flow-factors" role="region" aria-label="來源因子明細" tabindex="0">
      <table><thead><tr><th>因子</th><th>相關 r</th><th>樣本</th>${weights ? '<th>係數</th>' : ''}</tr></thead>
        <tbody>${measured.map(item=>`<tr><td>${safe(factorInfo(item.key)[0])}</td><td>${signed(item.correlation)}</td>
          <td>${integer(item.samples)}</td>${weights ? `<td>${signed(item.coefficient,3)}</td>` : ''}</tr>`).join('')}</tbody></table></div>
      <p class="flow-note">相關 r 以該驗證範圍的實際開盤方向計算。${weights ? '係數使用標準化輸入。' : ''}</p>`
    : '<p class="flow-note">這個版本尚無可計算的因子與權重。</p>'}`;
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
  const active = new Set(tracedEdges(links, selection));
  curves = [];

  for (const edge of [...links].sort((a,b)=>Number(active.has(a))-Number(active.has(b)))) {
    const from = panel.querySelector(`[data-node="${edge.from}"]`).getBoundingClientRect();
    const to = panel.querySelector(`[data-node="${edge.to}"]`).getBoundingClientRect();
    const x0 = from.right - box.left;
    const x1 = to.left - box.left;
    const y0 = (from.top + from.bottom)/2 - box.top;
    const y1 = (to.top + to.bottom)/2 - box.top;
    const middle = (x0+x1)/2;
    const info = state.stats[edge.from];
    const weight = finite(info?.impact_share) ? Number(info.impact_share) : 0;
    const missing = sources[edge.from] && !finite(info?.impact_share);
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

export function renderFlow(data, view) {
  const stats = { ...(data[view].diagnostics || {}) };
  if (view === 'week' && !stats.auction) {
    const observed = data.week.days.filter(day => finite(day.inputs) && day.expected);
    if (observed.length) stats.auction = {
      coverage:observed.reduce((sum,day)=>sum+day.inputs,0)/observed.reduce((sum,day)=>sum+day.expected,0)
    };
  }
  const historicalInputs = data[view].historical_inputs || {};
  state = { data, view, stats, historicalInputs, nodes:modelNodes(data,view) };
  if (view === 'week' && !state.stats[selection]) selection = 'auction';
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
