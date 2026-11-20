from io import BytesIO
from zipfile import ZipFile

import pandas as pd

from twquant.taifex_history import opening_tx_median


def test_opening_tx_uses_active_nearest_month_and_predecision_minute():
    header = '成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S)'
    rows = [
        '20261104,TX,202611,085801,900,2',
        '20261105,TX,202611,085759,901,2',
        '20261105,TX,202611,085801,100,2',
        '20261105,TX,202611,085858,102,2',
        '20261105,TX,202611,085859,950,2',
        '20261105,TX,202611,085900,951,2',
        '20261105,TX,202611W1,085801,952,2',
        '20261105,TX,202612,085801,953,2',
    ]
    content = BytesIO()
    with ZipFile(content, 'w') as archive:
        archive.writestr('daily.csv', '\n'.join([header, *rows]).encode('cp950'))
    assert opening_tx_median(content.getvalue(), pd.Timestamp('2026-11-05')) == ('202611', 101., 2)
