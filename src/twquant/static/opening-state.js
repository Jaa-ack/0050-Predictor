import { names } from './state.js';

const extraNames = {
  '1101':'台泥','1301':'台塑','1326':'台化','1476':'儒鴻','1590':'亞德客-KY','2207':'和泰車',
  '2301':'光寶科','2303':'聯電','2327':'國巨','2345':'智邦','2357':'華碩','2379':'瑞昱','2395':'研華',
  '2408':'南亞科','2603':'長榮','2609':'陽明','2615':'萬海','2882':'國泰金','2884':'玉山金',
  '2885':'元大金','2886':'兆豐金','2887':'台新新光金','2890':'永豐金','2892':'第一金',
  '3008':'大立光','3017':'奇鋐','3045':'台灣大','3231':'緯創','3443':'創意','3661':'世芯-KY',
  '3665':'貿聯-KY','3702':'大聯大','4904':'遠傳','4938':'和碩','5880':'合庫金','6415':'矽力-KY',
  '6446':'藥華藥','6505':'台塑化','6669':'緯穎','6781':'AES-KY','6919':'康霈','7769':'鴻勁',
  '8046':'南電','9910':'豐泰'
};
export const stockName = ticker => names[ticker] || extraNames[ticker] || '';

export function openingRows(rows, query, result) {
  const term = query.trim().toLowerCase();
  return rows.filter(row => {
    if (!`${row.ticker} ${stockName(row.ticker)}`.toLowerCase().includes(term)) return false;
    if (result === 'transfer') return row.transfer === true;
    if (row.constituent === false) return false;
    if (result === 'hit') return row.correct === true;
    if (result === 'miss') return row.correct === false;
    if (result === 'pending') return row.correct == null;
    return true;
  });
}

export const groupNames = {
  price:'台股價格', volume:'成交量', us:'美股與 ADR', night:'期貨夜盤', other:'籌碼與總經',
  auction:'盤前試撮', news:'公告與新聞', morning:'早盤跨市場', company:'公司事件'
};

const inputs = {
  f_return_1:['近 1 日報酬','percent'], f_return_5:['近 5 日報酬','percent'],
  f_return_20:['近 20 日報酬','percent'], f_return_60:['近 60 日報酬','percent'],
  f_gap:['前日跳空','percent'], f_intraday:['前日盤中報酬','percent'], f_range:['前日振幅','percent'],
  f_close_location:['收盤在區間內位置','percent'], f_volatility_20:['20 日波動','percent'],
  f_volatility_60:['60 日波動','percent'], f_price_ma20:['偏離 20 日均線','percent'], f_rsi_14:['14 日 RSI','percent'],
  f_market_return_1:['訓練股群近 1 日報酬','percent'], f_market_return_5:['訓練股群近 5 日報酬','percent'],
  f_peer_return_ex_target:['同儕近 1 日報酬','percent'], f_beta_60:['60 日市場敏感度','number'],
  f_rvol_20:['相對 20 日成交量','ratio'], f_amihud_20:['20 日非流動性','number'],
  f_flow_foreign_imbalance:['外資買賣超比','percent'], f_flow_foreign_mean5:['外資 5 日平均','percent'],
  f_flow_trust_imbalance:['投信買賣超比','percent'], f_margin_finance_utilization:['融資使用率','percent'],
  f_margin_short_utilization:['融券使用率','percent'], f_ext_fred_VIXCLS:['VIX 波動指數','number'],
  f_ext_fred_DEXTAUS:['美元兌臺幣（日頻）','number'], f_ext_fred_DGS10:['美債 10 年殖利率','rate'],
  f_open_auction_gap:['試撮相對前收','percent'], f_open_auction_imbalance:['試撮委買賣不平衡','percent'],
  f_open_auction_rvol:['相對試撮量','ratio'], f_open_official_count:['官方公告數','number'],
  f_open_media_count:['媒體事件數','number'], f_open_media_sentiment:['媒體情緒分數','number'],
  f_open_tx_morning:['臺指期早盤報酬','percent'], f_open_usdtwd_morning:['美元兌臺幣早盤變動','percent'],
  f_open_regional_equity_return:['區域市場早盤報酬','percent'],
  f_open_company_action_gap:['公司行動參考價差','percent'],
  f_open_earnings_surprise:['財報相對預期差異','number'], f_open_guidance_revision:['展望修正','number'],
  trial_gap:['試撮相對前收','percent'], log_matched_size:['試撮成交量','log-size'],
  book_imbalance:['試撮五檔不平衡','percent']
};

export function factorInfo(key) {
  if (inputs[key]) return inputs[key];
  const us = key.match(/^f_ext_(?:fmp|us_sector)_([a-z]+)_window_return$/);
  if (us) return [`${us[1].toUpperCase()} 隔夜報酬`,'percent'];
  const futures = key.match(/^f_ext_taifex_(tx|te|tf)_night_(return|range|volume_ratio)$/);
  if (futures) {
    const label = {tx:'臺指期',te:'電子期',tf:'金融期'}[futures[1]];
    const field = {return:'報酬',range:'振幅',volume_ratio:'量比'}[futures[2]];
    return [`${label}夜盤${field}`,futures[2] === 'volume_ratio' ? 'ratio' : 'percent'];
  }
  return [key,'number'];
}

export function openingEvidence(mode, kind, hasDay) {
  if (mode === 'snapshot' || kind === 'synthetic') return '合成示範';
  const replay = ['short_chronological_capture_replay', 'chronological_auction_comparison'].includes(kind);
  return hasDay && replay ? '實際收件重播 · 本組可計入實際發報 0／126 日' : '待驗證';
}
