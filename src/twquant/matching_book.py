"""Aggregate price-level matching with ordered fills and explicit finite depth.

No provider sessions or orders are connected. Depleting known depth loses book
completeness; the simulator never replenishes it with an invented reset quote.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .first_change import _wait


@dataclass(frozen=True)
class FillBatch:
    price:np.ndarray
    quantity:np.ndarray


@dataclass(frozen=True)
class BookResult:
    fills:tuple[FillBatch,...]
    cancelled:np.ndarray
    inserted:np.ndarray
    unfilled:np.ndarray


@dataclass(frozen=True)
class TimedOrders:
    at:np.ndarray
    kind:np.ndarray
    buy:np.ndarray
    quantity:np.ndarray
    price:np.ndarray
    ioc:np.ndarray


class BookBatch:
    """Independent books on a fixed tick grid: positive bids, negative asks.

    Kind0 is market,1 cancellation,2 limit; a limit may cross then rest or IOC.
    Aggregation preserves price-level execution order, not individual queue IDs.
    """
    def __init__(self,quantities,*,origin):
        q=np.asarray(quantities)
        if q.ndim!=2 or not len(q) or not 2<=q.shape[1]<=2048 or q.dtype.kind!='i' or (q<-10**9).any() or (q>10**9).any():raise ValueError('Declare a bounded signed integer book matrix')
        if type(origin) is not int or origin<1:raise ValueError('Declare a positive integer tick origin')
        self.quantities=q.astype('int64',copy=True);self.origin=origin
        self.bid_index=np.full(len(q),-1);self.ask_index=np.full(len(q),-1);self.complete=np.ones(len(q),bool)
        self._refresh(np.arange(len(q)),np.arange(len(q)))
        if not self.complete.all() or (self.bid_index>=self.ask_index).any():raise ValueError('Initial books need uncrossed bid and ask levels')

    def copy(self):
        other=object.__new__(BookBatch);other.origin=self.origin
        for name in ('quantities','bid_index','ask_index','complete'):setattr(other,name,getattr(self,name).copy())
        return other

    @property
    def bid_price(self):return np.where(self.bid_index>=0,self.origin+self.bid_index,np.nan)

    @property
    def ask_price(self):return np.where(self.ask_index>=0,self.origin+self.ask_index,np.nan)

    def _refresh(self,bids,asks):
        if len(bids):
            mask=self.quantities[bids]>0
            self.bid_index[bids]=np.where(mask.any(axis=1),self.quantities.shape[1]-1-mask[:,::-1].argmax(axis=1),-1)
        if len(asks):
            mask=self.quantities[asks]<0
            self.ask_index[asks]=np.where(mask.any(axis=1),mask.argmax(axis=1),-1)
        self.complete&=(self.bid_index>=0)&(self.ask_index>=0)

    def apply(self,*,kind,buy,quantity,price=None,ioc=False,active=None):
        N=len(self.quantities)
        def vector(value,name,types):
            a=np.asarray(value)
            if a.dtype.kind not in types:raise ValueError(f'Invalid {name} vector')
            try:return np.broadcast_to(a,(N,))
            except ValueError:raise ValueError(f'Invalid {name} shape') from None
        kinds=vector(kind,'kind','iu');buys=vector(buy,'buy','b');sizes=vector(quantity,'quantity','iu');immediate=vector(ioc,'ioc','b')
        prices=vector(np.nan if price is None else price,'price','fiu')
        use=self.complete.copy() if active is None else self.complete&vector(active,'active','b')
        if not np.isin(kinds,[0,1,2]).all() or (sizes<1).any() or (sizes>10**6).any():raise ValueError('Declare supported order kinds and positive bounded quantities')
        priced=use&(kinds!=0)
        if not (np.isfinite(prices[priced])&(prices[priced]==np.floor(prices[priced]))&(prices[priced]>=self.origin)&(prices[priced]<self.origin+self.quantities.shape[1])).all():raise ValueError('Order price is outside the known tick grid')
        index=np.where(priced,prices-self.origin,0).astype(int)
        cancelled=np.zeros(N,int);inserted=np.zeros(N,int);remaining=np.where(use&(kinds!=1),sizes,0).astype('int64');fills=[]
        ids=np.flatnonzero(use&(kinds==1));owned=self.quantities[ids,index[ids]]*np.where(buys[ids],1,-1)
        removed=np.minimum(sizes[ids],np.maximum(owned,0));cancelled[ids]=removed
        self.quantities[ids,index[ids]]-=np.where(buys[ids],removed,-removed)
        empty=self.quantities[ids,index[ids]]==0
        self._refresh(ids[empty&buys[ids]&(index[ids]==self.bid_index[ids])],ids[empty&~buys[ids]&(index[ids]==self.ask_index[ids])])
        while True:
            opposite=np.where(buys,self.ask_index,self.bid_index)
            available=use&(kinds!=1)&(remaining>0)&self.complete&(opposite>=0)
            quoted=self.origin+opposite
            crosses=(kinds==0)|np.where(buys,quoted<=prices,quoted>=prices)
            ids=np.flatnonzero(available&crosses)
            if not len(ids):break
            at=opposite[ids];taken=np.minimum(remaining[ids],np.abs(self.quantities[ids,at]))
            self.quantities[ids,at]+=np.where(buys[ids],taken,-taken);remaining[ids]-=taken
            values=np.full(N,np.nan);volumes=np.zeros(N,int);values[ids]=self.origin+at;volumes[ids]=taken
            fills.append(FillBatch(values,volumes))
            depleted=self.quantities[ids,at]==0
            self._refresh(ids[depleted&~buys[ids]],ids[depleted&buys[ids]])
        ids=np.flatnonzero(use&(kinds==2)&~immediate&(remaining>0)&self.complete)
        self.quantities[ids,index[ids]]+=np.where(buys[ids],remaining[ids],-remaining[ids]);inserted[ids]=remaining[ids];remaining[ids]=0
        better_bid=buys[ids]&(index[ids]>self.bid_index[ids]);better_ask=~buys[ids]&(index[ids]<self.ask_index[ids])
        self.bid_index[ids[better_bid]]=index[ids[better_bid]];self.ask_index[ids[better_ask]]=index[ids[better_ask]]
        return BookResult(tuple(fills),cancelled,inserted,remaining)

    def five_depths(self):
        N,W=self.quantities.shape;indices=np.arange(W)[None,:]
        bids=np.where(self.quantities>0,W-1-indices,W);asks=np.where(self.quantities<0,indices,W)
        # Only five actual nonempty prices; gaps do not become fictitious levels.
        count=min(5,W)
        b=np.sort(np.partition(bids,count-1,axis=1)[:,:count],axis=1);a=np.sort(np.partition(asks,count-1,axis=1)[:,:count],axis=1)
        bp=W-1-b;ap=a;rows=np.arange(N)[:,None]
        bq=np.where(b<W,self.quantities[rows,np.clip(bp,0,W-1)],0)
        aq=np.where(a<W,-self.quantities[rows,np.clip(ap,0,W-1)],0)
        result=np.zeros((N,2,5));result[:,0,:count]=bq;result[:,1,:count]=aq
        return result


def first_trade_change(book,last_trade,order_source,*,latency_seconds,max_wait_seconds):
    """Label the first changed individual fill after emission, on a copied book.

    The source receives that working book so later orders may use earlier quotes.
    An exhausted source is an unknown answer, never evidence of a timeout.
    """
    _wait(max_wait_seconds,target_kind='first_trade_change_before_deadline')
    if isinstance(latency_seconds,bool) or not isinstance(latency_seconds,(int,float)) or not np.isfinite(latency_seconds) or not 0<=latency_seconds<=5:raise ValueError('Invalid emission latency')
    work=book.copy();last=np.asarray(last_trade,dtype=float).copy();N=len(work.quantities)
    if last.shape!=(N,) or not np.isfinite(last).all() or (last<=0).any():raise ValueError('Declare the observed last trade for every book')
    resolved=np.zeros(N,bool);timeout=np.zeros(N,bool);lost=~work.complete.copy();anchored=np.zeros(N,bool)
    anchor=np.full(N,np.nan);actual=np.full(N,np.nan);first_price=np.full(N,np.nan);elapsed=np.full(N,np.nan);rank=np.full(N,np.nan)
    previous=np.zeros(N);pre_events=np.zeros(N,int);pre_changes=np.zeros(N,int);events=np.zeros(N,int)
    for order in order_source(work):
        active=~resolved&~lost
        if not active.any():break
        at=np.asarray(order.at,dtype=float)
        if at.shape!=(N,) or not np.isfinite(at).all() or (at[active]<previous[active]).any():raise ValueError('Timed orders must be finite and ordered per book')
        beyond=active&(at>latency_seconds+max_wait_seconds)
        resolved[beyond]=True;timeout[beyond]=True;actual[beyond]=0.;anchor[beyond]=last[beyond]
        use=active&~beyond;after=at>latency_seconds
        first=use&after&~anchored;anchor[first]=last[first];anchored[first]=True
        result=work.apply(kind=order.kind,buy=order.buy,quantity=order.quantity,price=order.price,ioc=order.ioc,active=use)
        pre_events[use&~after]+=1;events[use]+=1
        for number,fill in enumerate(result.fills,1):
            filled=use&(fill.quantity>0);changed=filled&(fill.price!=last)
            pre_changes[changed&~after]+=1
            hit=changed&after&~resolved
            actual[hit]=(fill.price[hit]-anchor[hit])/anchor[hit];first_price[hit]=fill.price[hit]
            elapsed[hit]=at[hit]-latency_seconds;rank[hit]=number;resolved[hit]=True
            last[filled]=fill.price[filled]
        lost|=~work.complete&~resolved;previous[use]=at[use]
    return dict(resolved=resolved,timeout=timeout,actual_return=actual,anchor_trade_price=anchor,first_change_price=first_price,
        event_after_emission_seconds=elapsed,first_change_fill_rank=rank,pre_emission_events=pre_events,
        pre_emission_price_changes=pre_changes,events_applied=events,book_incomplete=lost)
