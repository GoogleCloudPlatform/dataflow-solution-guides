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
"""Synthetic customer session data generator for testing and simulations."""

from datetime import datetime, timedelta, timezone
import random
from typing import Any, Dict, List, Tuple

from cdp_pipeline.models import (
    CouponRedemption,
    CustomerInteractionEvent,
    EventType,
    TransactionItem,
)


def generate_synthetic_session_events(
    household_key: str,
    base_tx_id: int,
    session_offset_sec: int = 0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  """Generates realistic transaction items and coupons for a customer shopping session."""
  tx_id = str(base_tx_id)
  event_time = datetime.now(
      timezone.utc) - timedelta(seconds=session_offset_sec)
  now_iso = event_time.isoformat()
  store_id = str(random.choice([101, 204, 305, 436]))

  # Products in session basket
  products = [
      {
          "id": "941769",
          "price": 3.99,
          "qty": 1,
          "disc": 0.50
      },
      {
          "id": "910635",
          "price": 2.99,
          "qty": 2,
          "disc": 0.00
      },
      {
          "id": "1082185",
          "price": 1.49,
          "qty": 1,
          "disc": 0.25
      },
  ]
  selected = random.sample(products, k=random.randint(1, len(products)))

  transactions = []
  for p in selected:
    tx_item = TransactionItem(
        product_id=p["id"],
        quantity=p["qty"],
        sales_value=round(p["price"] * p["qty"], 2),
        store_id=store_id,
        retail_disc=0.0,
        coupon_disc=p["disc"],
        coupon_match_disc=0.0,
        day=421,
        week_no=8,
        trans_time="1456",
    )
    tx_event = CustomerInteractionEvent(
        event_type=EventType.TRANSACTION.value,
        household_key=household_key,
        transaction_id=tx_id,
        event_timestamp=now_iso,
        transaction=tx_item,
        coupon=None,
    )
    transactions.append({
        "household_key": tx_event.household_key,
        "transaction_id": tx_event.transaction_id,
        "product_id": tx_item.product_id,
        "quantity": tx_item.quantity,
        "sales_value": tx_item.sales_value,
        "store_id": tx_item.store_id,
        "retail_disc": tx_item.retail_disc,
        "coupon_disc": tx_item.coupon_disc,
        "coupon_match_disc": tx_item.coupon_match_disc,
        "day": tx_item.day,
        "week_no": tx_item.week_no,
        "trans_time": tx_item.trans_time,
        "event_timestamp": tx_event.event_timestamp,
    })

  coupons = []
  if random.random() < 0.7:  # 70% chance of coupon redemption
    coupon_item = CouponRedemption(
        coupon_upc=str(random.choice([10000085364, 51700010076, 10000089277])),
        campaign=str(random.choice([2200, 18, 500])),
        day=421,
    )
    cp_event = CustomerInteractionEvent(
        event_type=EventType.COUPON.value,
        household_key=household_key,
        transaction_id=tx_id,
        event_timestamp=now_iso,
        transaction=None,
        coupon=coupon_item,
    )
    coupons.append({
        "household_key": cp_event.household_key,
        "transaction_id": cp_event.transaction_id,
        "coupon_upc": coupon_item.coupon_upc,
        "campaign": coupon_item.campaign,
        "day": coupon_item.day,
        "event_timestamp": cp_event.event_timestamp,
    })

  return transactions, coupons
