"""The user-selected ten are immutable training assets; other symbols are tests."""
import re

TRAINING_TICKERS=('2330','2454','2317','2308','3711','1303','2383','2881','2891','3037')
BENCHMARK='0050'
MARKET_INDEX='IX0001'
DEFAULT_EVALUATION_TICKERS=(BENCHMARK,'1216','2002','2412',MARKET_INDEX)


def tickers(values):
    result=tuple(dict.fromkeys(str(value).strip() for value in values))
    if not result or any(not re.fullmatch(r'[A-Za-z0-9^_.!-]{1,40}',value) for value in result):
        raise ValueError('Provide nonempty valid instrument identifiers')
    return result


def asset_class(ticker):
    return 'index' if str(ticker).startswith(('IX','IR')) else 'etf' if str(ticker).startswith('00') else 'equity'


def evaluation_role(ticker):
    return 'fixed_training_universe' if ticker in TRAINING_TICKERS else 'benchmark_transfer' if ticker==BENCHMARK else 'market_transfer' if asset_class(ticker)=='index' else 'external_stock_transfer'
