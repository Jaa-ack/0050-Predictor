import test from 'node:test';
import assert from 'node:assert/strict';

const state = await import('../src/twquant/static/state.js').catch(() => ({}));

test('latest forecasts use decision timestamps rather than API ordering and retain the selected horizon', () => {
  assert.equal(typeof state.latestForecasts, 'function', 'chronological forecast selection must be available');
  const old = {ticker:'2330', horizon:1, anchor_date:'2025-01-02', decision_ts:'2025-01-02T09:00:00Z'};
  const latest = {...old, decision_ts:'2025-01-02T10:00:00Z'};
  const other = {...old,ticker:'2454'};
  assert.deepEqual(state.latestForecasts([old,{...old,horizon:5},latest,other],1),[latest,other]);
});

test('forecast filtering accepts ticker and Chinese company name without changing the source array', () => {
  assert.equal(typeof state.filterForecasts,'function');
  const rows=[{ticker:'2330',horizon:1},{ticker:'2454',horizon:1},{ticker:'2330',horizon:5}];
  assert.deepEqual(state.filterForecasts(rows,{horizon:1,query:' 台積 '}),[rows[0]]);
  assert.deepEqual(state.filterForecasts(rows,{horizon:5,query:'2330'}),[rows[2]]);
  assert.equal(rows.length,3);
});

test('formatters preserve real zero and withhold absent or nonfinite metrics', () => {
  assert.equal(typeof state.percent,'function');
  assert.equal(state.percent(0),'0.00%');
  for(const missing of [null,undefined,NaN,Infinity,'']) assert.equal(state.percent(missing),'—');
  assert.equal(state.decimal(-0.01234,3),'-0.012');
});

test('chart geometry preserves missing observations as gaps and handles a flat series', () => {
  assert.equal(typeof state.lineGeometry,'function');
  const result=state.lineGeometry([{v:1},{v:null},{v:1},{v:1}],['v'],{width:400,height:200});
  assert.equal((result.paths.v.match(/M/g)||[]).length,2);
  assert.ok(!/NaN|Infinity/.test(result.paths.v));
  assert.ok(result.max>result.min);
  assert.equal(state.lineGeometry([],['v']).paths.v,'');
});

test('explanations accept only a finite numeric baseline contribution and never evaluate source text', () => {
  assert.equal(typeof state.factors,'function');
  const source=JSON.stringify([{feature:'return_20',linear_contribution:-0.02},{feature:'invalid',linear_contribution:null}]);
  assert.deepEqual(state.factors(source),[{feature:'return_20',value:-0.02}]);
  assert.deepEqual(state.factors('not JSON'),[]);
});

test('snapshot endpoint paths contain no action endpoint and support an encoded ticker', () => {
  assert.equal(typeof state.endpoint,'function');
  assert.equal(state.endpoint({mode:'snapshot'},'prices','demo',{ticker:'2330'}),'./data/prices-2330.json');
  assert.equal(state.endpoint({mode:'snapshot'},'forecasts','demo'),'./data/forecasts.json');
  assert.throws(()=>state.endpoint({mode:'snapshot'},'session','demo'),/local|snapshot/i);
  assert.equal(state.endpoint({mode:'local'},'prices','demo',{ticker:'2330'}),'./api/prices?workspace=demo&ticker=2330');
});

test('gap filtering keeps completed work visible only under the matching status and priority', () => {
  assert.equal(typeof state.filterGaps,'function');
  const rows=[{priority:'P0',status:'pending'},{priority:'P1',status:'complete'},{priority:'P0',status:'blocked'}];
  assert.deepEqual(state.filterGaps(rows,'P0'),[rows[0],rows[2]]);
  assert.deepEqual(state.filterGaps(rows,'all'),rows);
});
