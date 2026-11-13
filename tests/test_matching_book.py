import importlib.util

import numpy as np
import pytest


def module():
    assert importlib.util.find_spec('twquant.matching_book') is not None, 'Ordered level matching and individual fills are missing'
    from twquant import matching_book
    return matching_book


def book():
    q=np.zeros((1,24),dtype=int)
    for price,size in ((98,10),(99,7)):q[0,price-90]=size
    for price,size in ((100,2),(101,3),(102,8),(103,9)):q[0,price-90]=-size
    return module().BookBatch(q,origin=90)


def fills(result):
    return [(int(f.price[0]),int(f.quantity[0])) for f in result.fills if f.quantity[0]]


def test_market_buy_walks_opposite_levels_and_preserves_the_bid():
    b=book();before=np.abs(b.quantities).sum()
    r=b.apply(kind=[0],buy=[True],quantity=[4])
    assert fills(r)==[(100,2),(101,2)] and r.unfilled[0]==0
    assert b.bid_price[0]==99 and b.ask_price[0]==101
    assert b.quantities[0,101-90]==-1
    assert np.abs(b.quantities).sum()==before-4 and b.complete[0]


def test_ioc_cancels_remainder_and_resting_limit_retains_it_at_its_price():
    for ioc in (True,False):
        b=book();before=np.abs(b.quantities).sum()
        r=b.apply(kind=[2],buy=[True],quantity=[5],price=[100],ioc=[ioc])
        assert fills(r)==[(100,2)]
        assert b.ask_price[0]==101 and b.bid_price[0]==(99 if ioc else 100)
        assert r.unfilled[0]==(3 if ioc else 0) and r.inserted[0]==(0 if ioc else 3)
        assert np.abs(b.quantities).sum()==before-2+r.inserted[0]


def test_sell_partial_fills_and_cancellation_update_only_the_relevant_quote():
    b=book();r=b.apply(kind=[0],buy=[False],quantity=[9])
    assert fills(r)==[(99,7),(98,2)] and b.bid_price[0]==98 and b.ask_price[0]==100
    assert b.quantities[0,98-90]==8
    r=b.apply(kind=[1],buy=[False],quantity=[2],price=[100])
    assert not r.fills and r.cancelled[0]==2 and b.ask_price[0]==101 and b.bid_price[0]==98
    r=b.apply(kind=[2],buy=[False],quantity=[4],price=[100])
    assert not r.fills and b.ask_price[0]==100 and b.bid_price[0]==98


def test_exhausted_known_book_is_incomplete_and_never_invents_a_quote():
    b=book();r=b.apply(kind=[0],buy=[True],quantity=[30])
    assert fills(r)==[(100,2),(101,3),(102,8),(103,9)] and r.unfilled[0]==8
    assert not b.complete[0] and np.isnan(b.ask_price[0]) and b.bid_price[0]==99
    before=b.quantities.copy();b.apply(kind=[0],buy=[False],quantity=[1])
    np.testing.assert_array_equal(before,b.quantities)


def timed(at,buy,quantity,kind=0,price=np.nan):
    return module().TimedOrders(np.array([at]),np.array([kind]),np.array([buy]),np.array([quantity]),np.array([price]),np.array([False]))


def test_emission_anchor_uses_the_last_individual_fill_and_observed_book_is_immutable():
    m=module();b=book();before=b.quantities.copy()
    orders=[timed(.002,True,3),timed(.007,False,1)]
    r=m.first_trade_change(b,np.array([100.]),lambda working:iter(orders),latency_seconds=.005,max_wait_seconds=60)
    assert r['resolved'][0] and not r['timeout'][0] and r['anchor_trade_price'][0]==101
    assert r['first_change_price'][0]==99 and r['actual_return'][0]==pytest.approx(-2/101)
    assert r['event_after_emission_seconds'][0]==pytest.approx(.002)
    np.testing.assert_array_equal(before,b.quantities)


def test_same_price_fill_precedes_the_first_changed_fill_in_the_same_order():
    m=module();b=book()
    # Three shares before emission leave two at101; one later same-price print,
    # then the next order finishes101 and first changes at102.
    orders=[timed(.002,True,3),timed(.007,True,1),timed(.010,True,2)]
    r=m.first_trade_change(b,np.array([100.]),lambda working:iter(orders),latency_seconds=.005,max_wait_seconds=60)
    assert r['anchor_trade_price'][0]==101 and r['first_change_price'][0]==102
    assert r['first_change_fill_rank'][0]==2 and r['event_after_emission_seconds'][0]==pytest.approx(.005)
    assert r['actual_return'][0]==pytest.approx(1/101)
    unknown=m.first_trade_change(b,np.array([100.]),lambda working:iter([timed(.001,True,1)]),latency_seconds=0.,max_wait_seconds=60)
    assert not unknown['resolved'][0] and not unknown['timeout'][0] and np.isnan(unknown['actual_return'][0])
    timeout=m.first_trade_change(b,np.array([100.]),lambda working:iter([timed(61.,True,1)]),latency_seconds=0.,max_wait_seconds=60)
    assert timeout['resolved'][0] and timeout['timeout'][0] and timeout['actual_return'][0]==0
    lost=m.first_trade_change(b,np.array([100.]),lambda working:iter([timed(.001,True,30),timed(61.,True,1)]),latency_seconds=.005,max_wait_seconds=60)
    assert not lost['resolved'][0] and not lost['timeout'][0] and np.isnan(lost['anchor_trade_price'][0])


def test_minimum_integer_cannot_bypass_book_quantity_bounds():
    b=book();q=b.quantities.copy();q[0,12]=np.iinfo('int64').min
    with pytest.raises(ValueError,match='bounded'):module().BookBatch(q,origin=90)
