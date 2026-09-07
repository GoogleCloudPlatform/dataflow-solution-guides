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
"""Data models and Beam schemas for the Customer Data Platform pipeline."""

from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, Union


class EventType(StrEnum):
  """Enumerates customer interaction event types."""
  TRANSACTION = "transaction"
  COUPON = "coupon"


class TransactionItem(NamedTuple):
  """Granular basket item in a customer transaction."""
  product_id: Optional[str] = None
  quantity: int = 1
  sales_value: float = 0.0
  store_id: Optional[str] = None
  retail_disc: float = 0.0
  coupon_disc: float = 0.0
  coupon_match_disc: float = 0.0
  day: Optional[int] = None
  week_no: Optional[int] = None
  trans_time: Optional[str] = None

  def to_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation of the transaction item."""
    return self._asdict()


class CouponRedemption(NamedTuple):
  """Coupon redemption attached to a customer transaction."""
  coupon_upc: Optional[str] = None
  campaign: Optional[str] = None
  day: Optional[int] = None

  def to_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation of the coupon redemption."""
    return self._asdict()


class DeadLetterRecord(NamedTuple):
  """Represents a malformed or invalid payload routed to the Dead-Letter Queue."""
  source: str
  raw_payload: str
  error_message: str
  timestamp: str

  def to_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation suitable for BigQuery insertion."""
    return self._asdict()


class CustomerInteractionEvent(NamedTuple):
  """Unified customer event composed of transaction or coupon data for sessionization."""
  event_type: str  # EventType.TRANSACTION or EventType.COUPON
  household_key: str
  transaction_id: str
  event_timestamp: Optional[str] = None
  transaction: Optional[TransactionItem] = None
  coupon: Optional[CouponRedemption] = None

  def to_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation of the event."""
    return {
        "event_type": self.event_type,
        "household_key": self.household_key,
        "transaction_id": self.transaction_id,
        "event_timestamp": self.event_timestamp,
        "transaction": self.transaction.to_dict() if self.transaction else None,
        "coupon": self.coupon.to_dict() if self.coupon else None,
    }

  @classmethod
  def from_raw_payload(
      cls,
      element: Union[bytes, str, Dict[str, Any]],
      expected_type: EventType,
  ) -> Tuple[Optional["CustomerInteractionEvent"], Optional[DeadLetterRecord]]:
    """Decodes and validates raw bytes/str/dict into a typed CustomerInteractionEvent."""
    try:
      if isinstance(element, bytes):
        payload_str = element.decode("utf-8")
        data = json.loads(payload_str)
      elif isinstance(element, str):
        payload_str = element
        data = json.loads(payload_str)
      elif isinstance(element, dict):
        payload_str = json.dumps(element)
        data = dict(element)
      else:
        raise ValueError(f"Unsupported payload type: {type(element)}")
    except Exception as exc:  # pylint: disable=broad-exception-caught
      now_iso = datetime.now(timezone.utc).isoformat()
      return None, DeadLetterRecord(
          source=expected_type.value,
          raw_payload=str(element)[:2000],
          error_message=f"Malformed payload: {exc}",
          timestamp=now_iso,
      )

    hh_key = str(data.get("household_key", "")).strip()
    tx_id = str(data.get("transaction_id", "")).strip()
    if not hh_key or not tx_id:
      now_iso = datetime.now(timezone.utc).isoformat()
      return None, DeadLetterRecord(
          source=expected_type.value,
          raw_payload=payload_str[:2000],
          error_message="Missing required household_key or transaction_id",
          timestamp=now_iso,
      )

    event_ts = data.get("event_timestamp")

    if expected_type == EventType.TRANSACTION:
      try:
        qty = int(float(data.get("quantity", 1) or 1))
      except (ValueError, TypeError):
        qty = 1
      try:
        sales = float(data.get("sales_value", 0.0) or 0.0)
      except (ValueError, TypeError):
        sales = 0.0
      try:
        ret_disc = float(data.get("retail_disc", 0.0) or 0.0)
      except (ValueError, TypeError):
        ret_disc = 0.0
      try:
        coup_disc = float(
            data.get("coupon_disc", data.get("coupon_discount", 0.0)) or 0.0)
      except (ValueError, TypeError):
        coup_disc = 0.0
      try:
        match_disc = float(data.get("coupon_match_disc", 0.0) or 0.0)
      except (ValueError, TypeError):
        match_disc = 0.0

      day_val = data.get("day")
      week_val = data.get("week_no")

      tx_item = TransactionItem(
          product_id=str(data.get("product_id", "")).strip() or None,
          quantity=qty,
          sales_value=sales,
          store_id=str(data.get("store_id", "")).strip() or None,
          retail_disc=ret_disc,
          coupon_disc=coup_disc,
          coupon_match_disc=match_disc,
          day=int(day_val) if day_val is not None else None,
          week_no=int(week_val) if week_val is not None else None,
          trans_time=str(data.get("trans_time", "")).strip() or None,
      )
      return cls(
          event_type=EventType.TRANSACTION.value,
          household_key=hh_key,
          transaction_id=tx_id,
          event_timestamp=event_ts,
          transaction=tx_item,
          coupon=None,
      ), None

    # Coupon event
    day_val = data.get("day")
    coupon_item = CouponRedemption(
        coupon_upc=str(data.get("coupon_upc", "")).strip() or None,
        campaign=str(data.get("campaign", "")).strip() or None,
        day=int(day_val) if day_val is not None else None,
    )
    return cls(
        event_type=EventType.COUPON.value,
        household_key=hh_key,
        transaction_id=tx_id,
        event_timestamp=event_ts,
        transaction=None,
        coupon=coupon_item,
    ), None


class UnifiedTransactionRecord(NamedTuple):
  """Granular customer transaction item enriched with session ID and discounts."""
  session_id: str
  transaction_id: str
  household_key: str
  product_id: Optional[str]
  quantity: Optional[int]
  sales_value: Optional[float]
  store_id: Optional[str]
  retail_disc: Optional[float]
  coupon_discount: Optional[float]
  coupon_match_disc: Optional[float]
  coupon_upc: Optional[str]
  campaign: Optional[str]
  day: Optional[int]
  trans_time: Optional[str]
  week_no: Optional[int]
  event_timestamp: Optional[str]
  processed_timestamp: str

  def to_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation suitable for BigQuery insertion."""
    return self._asdict()


class CustomerSessionProfile(NamedTuple):
  """Aggregated Customer 360 session profile."""
  session_id: str
  household_key: str
  session_start: str
  session_end: str
  session_duration_sec: int
  total_transactions: int
  total_items_purchased: int
  total_spend: float
  total_discount: float
  coupons_redeemed_count: int
  distinct_products_count: int
  campaigns: List[str]
  stores_visited: List[str]
  processed_timestamp: str

  def to_dict(self) -> Dict[str, Any]:
    """Returns a dictionary representation suitable for BigQuery insertion."""
    return self._asdict()
