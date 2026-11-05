export const names = {'2330':'台積電','2454':'聯發科','2317':'鴻海','2308':'台達電','3711':'日月光投控','1303':'南亞','2383':'台光電','2881':'富邦金','2891':'中信金','3037':'欣興','0050':'元大台灣50'};
export const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
export const percent = (value,digits=2) => finite(value) ? `${(Number(value)*100).toFixed(digits)}%` : '—';
export const decimal = (value,digits=4) => finite(value) ? Number(value).toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits}) : '—';
export const integer = value => finite(value) ? Number(value).toLocaleString('en-US',{maximumFractionDigits:0}) : '—';

export function forecastEvidence(row) {
  if(row.data_kind==='synthetic') return '合成示範';
  if(row.is_replay===true) return '歷史重播';
  if(row.data_kind==='historical' && row.is_replay===false && Number.isFinite(Date.parse(row.issued_at)) && Number.isFinite(Date.parse(row.decision_ts))) return '即時簽發';
  return '簽發待核對';
}
export function holdoutMetrics(report) {
  return report?.metrics?.sealed_holdout?.metrics || null;
}

export function latestForecasts(rows,horizon) {
  const latest=new Map();
  for(const row of rows) {
    if(Number(row.horizon)!==Number(horizon)) continue;
    const existing=latest.get(String(row.ticker));
    const stamp=`${row.anchor_date||''}|${row.decision_ts||''}|${row.issued_at||''}`;
    const previous=existing ? `${existing.anchor_date||''}|${existing.decision_ts||''}|${existing.issued_at||''}` : '';
    if(!existing || stamp>previous) latest.set(String(row.ticker),row);
  }
  return [...latest.values()].sort((a,b)=>String(a.ticker).localeCompare(String(b.ticker)));
}
export function filterForecasts(rows,{horizon=1,query=''}={}) {
  const term=query.trim().toLowerCase();
  return latestForecasts(rows,horizon).filter(row=>`${row.ticker} ${names[row.ticker]||''}`.toLowerCase().includes(term));
}
export function factors(value) {
  try {
    const parsed=typeof value==='string'?JSON.parse(value):value;
    return Array.isArray(parsed) ? parsed.filter(row=>row && typeof row.feature==='string' && finite(row.linear_contribution))
      .map(row=>({feature:row.feature,value:Number(row.linear_contribution)})).sort((a,b)=>Math.abs(b.value)-Math.abs(a.value)) : [];
  } catch { return []; }
}
export function filterGaps(rows,priority='all') {
  return priority==='all' ? [...rows] : rows.filter(row=>String(row.priority).toUpperCase()===priority.toUpperCase());
}
export function endpoint(config,name,workspace,params={}) {
  if(config.mode==='snapshot') {
    if(name==='session') throw new Error('session is local only; unavailable in snapshot');
    return `./data/${name==='prices'?`prices-${encodeURIComponent(params.ticker)}`:name}.json`;
  }
  const query=new URLSearchParams({workspace,...params});
  return `./api/${name}?${query}`;
}
export function lineGeometry(rows,keys,{width=800,height=280,left=52,right=18,top=18,bottom=30}={}) {
  const values=rows.flatMap(row=>keys.map(key=>row[key]).filter(finite).map(Number));
  let min=values.length?Math.min(...values):0,max=values.length?Math.max(...values):1;
  if(min===max) { const pad=Math.max(Math.abs(min)*.02,.01);min-=pad;max+=pad; }
  else {const pad=(max-min)*.1;min-=pad;max+=pad;}
  const x=index=>left+index/Math.max(1,rows.length-1)*(width-left-right);
  const y=value=>top+(max-Number(value))/(max-min)*(height-top-bottom);
  const paths={};
  for(const key of keys) {
    let gap=true,path='';
    rows.forEach((row,index)=>{if(!finite(row[key])){gap=true;return;}path+=`${gap?'M':'L'}${x(index).toFixed(2)},${y(row[key]).toFixed(2)} `;gap=false;});
    paths[key]=path.trim();
  }
  return {paths,min,max,x,y,width,height,left,right,top,bottom};
}
