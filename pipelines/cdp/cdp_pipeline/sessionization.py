#  Copyright 2026 Google LLC
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""Session aggregation and Customer 360 profile generation transforms."""

import collections
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import apache_beam as beam
from apache_beam import PCollection
from apache_beam.metrics import Metrics

from cdp_pipeline.models import (
    CouponRedemption,
    CustomerInteractionEvent,
    CustomerSessionProfile,
    TransactionItem,
    UnifiedTransactionRecord,
)

TAG_SESSIONS = "sessions"


class ProcessCustomerSessionDoFn(beam.DoFn):
  """Aggregates customer interactions into unified records and Customer 360 profiles."""

  def __init__(self):
    super().__init__()
    self.sessions_counter = None
    self.unified_items_counter = None
    self.matched_coupons_counter = None

  def setup(self):
    self.sessions_counter = Metrics.counter(self.__class__,
                                            "completed_sessions")
    self.unified_items_counter = Metrics.counter(self.__class__,
                                                 "unified_items_emitted")
    self.matched_coupons_counter = Metrics.counter(self.__class__,
                                                   "matched_coupons")

  def process(
      self,
      element: Tuple[str, Iterable[CustomerInteractionEvent]],
      window=beam.DoFn.WindowParam,
  ) -> Generator[Any, None, None]:
    household_key, events_iter = element
    events = list(events_iter)
    if not events:
      return

    try:
      session_start_iso = window.start.to_utc_datetime().isoformat()
    except (OverflowError, ValueError):
      session_start_iso = datetime.now(timezone.utc).isoformat()

    try:
      session_end_iso = window.end.to_utc_datetime().isoformat()
    except (OverflowError, ValueError):
      session_end_iso = datetime.now(timezone.utc).isoformat()

    try:
      session_duration_sec = max(
          0, int((window.end.micros - window.start.micros) / 1000000))
    except (OverflowError, ValueError):
      session_duration_sec = 0

    try:
      start_sec = int(window.start.micros / 1000000)
    except (OverflowError, ValueError):
      start_sec = int(datetime.now(timezone.utc).timestamp())
    session_id = f"sess_{household_key}_{start_sec}"
    processed_ts = datetime.now(timezone.utc).isoformat()

    transactions: List[Tuple[str, TransactionItem, Optional[str]]] = []
    coupons_by_tx: Dict[str,
                        List[CouponRedemption]] = collections.defaultdict(list)

    for ev in events:
      if ev.transaction is not None:
        transactions.append(
            (ev.transaction_id, ev.transaction, ev.event_timestamp))
      elif ev.coupon is not None:
        coupons_by_tx[ev.transaction_id].append(ev.coupon)

    distinct_tx_ids = set()
    distinct_products = set()
    campaigns = set()
    stores = set()
    total_spend = 0.0
    total_items = 0
    total_discount = 0.0
    coupons_redeemed_count = 0

    for coupon_list in coupons_by_tx.values():
      coupons_redeemed_count += len(coupon_list)
      for c in coupon_list:
        if c.campaign:
          campaigns.add(c.campaign)

    for tx_id, tx, event_ts in transactions:
      distinct_tx_ids.add(tx_id)
      if tx.product_id:
        distinct_products.add(tx.product_id)
      if tx.store_id:
        stores.add(tx.store_id)

      total_spend += tx.sales_value
      total_items += tx.quantity
      total_discount += (tx.retail_disc + tx.coupon_disc)

      matching_coupons = coupons_by_tx.get(tx_id, [])
      if not matching_coupons:
        coupon_iter: List[Optional[CouponRedemption]] = [None]
      else:
        coupon_iter = matching_coupons
        self.matched_coupons_counter.inc(len(matching_coupons))

      for coup in coupon_iter:
        coupon_upc = coup.coupon_upc if coup else None
        campaign = coup.campaign if coup else None

        unified_record = UnifiedTransactionRecord(
            session_id=session_id,
            transaction_id=tx_id,
            household_key=household_key,
            product_id=tx.product_id,
            quantity=tx.quantity,
            sales_value=tx.sales_value,
            store_id=tx.store_id,
            retail_disc=tx.retail_disc,
            coupon_discount=tx.coupon_disc,
            coupon_match_disc=tx.coupon_match_disc,
            coupon_upc=coupon_upc,
            campaign=campaign,
            day=tx.day,
            trans_time=tx.trans_time,
            week_no=tx.week_no,
            event_timestamp=event_ts or processed_ts,
            processed_timestamp=processed_ts,
        )
        yield unified_record
        self.unified_items_counter.inc()

    session_profile = CustomerSessionProfile(
        session_id=session_id,
        household_key=household_key,
        session_start=session_start_iso,
        session_end=session_end_iso,
        session_duration_sec=session_duration_sec,
        total_transactions=len(distinct_tx_ids),
        total_items_purchased=total_items,
        total_spend=round(total_spend, 2),
        total_discount=round(total_discount, 2),
        coupons_redeemed_count=coupons_redeemed_count,
        distinct_products_count=len(distinct_products),
        campaigns=sorted(list(campaigns)),
        stores_visited=sorted(list(stores)),
        processed_timestamp=processed_ts,
    )
    yield beam.pvalue.TaggedOutput(TAG_SESSIONS, session_profile)
    self.sessions_counter.inc()


def left_join(
    key_value_pair: Tuple[Any, Tuple[Iterable[Dict[str, Any]],
                                     Iterable[Optional[Dict[str, Any]]]]]
) -> Generator[Dict[str, Any], None, None]:
  """Legacy helper performing a left join between transaction and coupon redemption records."""
  _, values = key_value_pair
  trans_values, coupon_redempt_values = values
  coupon_list = list(coupon_redempt_values)
  if not coupon_list:
    coupon_list = [None]
  for trans_value in trans_values:
    if trans_value is not None:
      for coupon_redempt_value in coupon_list:
        coupon_upc = None
        if isinstance(coupon_redempt_value, dict):
          raw_upc = coupon_redempt_value.get("coupon_upc")
          if raw_upc is not None:
            coupon_upc = str(raw_upc)
        unified_data = {
            "transaction_id":
                str(trans_value["transaction_id"]),
            "household_key":
                str(trans_value["household_key"]),
            "coupon_upc":
                coupon_upc,
            "product_id":
                str(trans_value["product_id"]),
            "coupon_discount":
                str(
                    trans_value.get("coupon_disc",
                                    trans_value.get("coupon_discount", "0"))),
        }
        yield unified_data


@beam.ptransform_fn
def _unify_data(
    pcolls: Tuple[PCollection, PCollection]) -> PCollection[Dict[str, Any]]:
  """Legacy transform combining transactions and coupons via CoGroupByKey."""
  transactions_pcoll, coupons_redempt_pcoll = pcolls
  unified_data = ((transactions_pcoll, coupons_redempt_pcoll)
                  | "Combine Transactions and Coupons" >> beam.CoGroupByKey()
                  | beam.FlatMap(left_join))
  return unified_data
