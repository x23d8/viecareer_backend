"""Billing and payOS payment handling."""

from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException, status
from psycopg2.extras import Json

from config import (
    APP_ENV,
    AUTH_MODE,
    DEV_USER_EMAIL,
    DEV_USER_ID,
    DEV_USER_NAME,
    PAYOS_API_BASE,
    PAYOS_API_KEY,
    PAYOS_CANCEL_URL,
    PAYOS_CHECKSUM_KEY,
    PAYOS_CLIENT_ID,
    PAYOS_RETURN_URL,
)
from core.database import get_connection
from services.plan_quota_service import ensure_plan_quota_schema, get_plan_quota_summary_sync
from services.user_service import upsert_auth_user

logger = logging.getLogger("viecareer")

PAYMENT_PROVIDER_PAYOS = "payos"
PAYMENT_STATUS_PENDING = "pending"
PAYMENT_STATUS_PAID = "paid"
PAYMENT_STATUS_CANCELLED = "cancelled"
PAYMENT_STATUS_EXPIRED = "expired"
PAYMENT_STATUS_FAILED = "failed"

CHECKOUT_PLAN_CODES = {"standard", "pro", "team", "usage_based"}
RECURRING_PLAN_CODES = {"standard", "pro", "team"}
PLAN_PRIORITY = {"team": 400, "pro": 300, "standard": 200, "free": 100, "usage_based": 50}

PAYOS_PAID_STATUS = "PAID"
PAYOS_CANCELLED_STATUS = "CANCELLED"
PAYOS_EXPIRED_STATUS = "EXPIRED"

_schema_ready = False


async def create_payos_checkout(
    *,
    external_auth_id: str,
    email: str | None,
    plan_code: str,
) -> dict[str, Any]:
    plan_code = _normalize_plan_code(plan_code)
    if plan_code not in CHECKOUT_PLAN_CODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This plan cannot be purchased.")

    _require_payos_config()
    payment = _create_pending_payment(external_auth_id=external_auth_id, email=email, plan_code=plan_code)

    request_payload = _payos_create_payment_payload(payment)
    try:
        payos_response = await _payos_request("POST", "/v2/payment-requests", json_body=request_payload)
        data = payos_response.get("data") or {}
        if payos_response.get("code") != "00" or not data.get("checkoutUrl"):
            _mark_payment_failed(payment["id"], payos_response)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="payOS could not create a checkout link.")

        if payos_response.get("signature") and not _verify_payos_signature(data, str(payos_response["signature"])):
            _mark_payment_failed(payment["id"], payos_response)
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="payOS response signature is invalid.")

        updated = _attach_payos_checkout_data(payment["id"], request_payload, payos_response)
    except HTTPException:
        raise
    except Exception as exc:
        _mark_payment_failed(payment["id"], {"error": str(exc)})
        logger.exception("payOS checkout creation failed for payment=%s", payment["id"])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Failed to create payOS checkout link.") from exc

    return {
        "payment_id": updated["id"],
        "order_code": updated["order_code"],
        "plan_code": payment["plan_code"],
        "amount_vnd": int(updated["amount_vnd"]),
        "status": updated["status"],
        "checkout_url": updated["checkout_url"],
        "payment_link_id": updated["payment_link_id"],
        "expires_at": updated["expires_at"],
    }


def get_billing_overview(*, external_auth_id: str, email: str | None) -> dict[str, Any]:
    ensure_billing_schema()
    expire_due_subscriptions_sync()
    quota_summary = get_plan_quota_summary_sync(external_auth_id=external_auth_id)

    with get_connection() as conn:
        with conn.cursor() as cur:
            user = _lookup_user(cur, external_auth_id=external_auth_id, email=email)
            plans = _list_plans(cur)
            free_plan = next((plan for plan in plans if plan["code"] == "free"), None)
            if not user:
                return {
                    "current_plan": free_plan,
                    "subscription": None,
                    "plans": plans,
                    "recent_payments": [],
                    "quota_summary": quota_summary,
                }

            subscription = _current_subscription(cur, user["id"])
            current_plan = subscription["plan"] if subscription else free_plan
            payments = _payment_history_for_user(cur, user["id"], limit=5)

    return {
        "current_plan": current_plan,
        "subscription": subscription,
        "plans": plans,
        "recent_payments": payments,
        "quota_summary": quota_summary,
    }


def list_payment_history(*, external_auth_id: str, email: str | None, limit: int = 50) -> list[dict[str, Any]]:
    ensure_billing_schema()
    expire_due_subscriptions_sync()

    with get_connection() as conn:
        with conn.cursor() as cur:
            user = _lookup_user(cur, external_auth_id=external_auth_id, email=email)
            if not user:
                return []
            return _payment_history_for_user(cur, user["id"], limit=limit)


async def sync_payos_order(
    *,
    external_auth_id: str,
    email: str | None,
    order_code: int,
) -> dict[str, Any]:
    _require_payos_config()
    payment = _payment_for_authenticated_user(external_auth_id=external_auth_id, email=email, order_code=order_code)
    if not payment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Payment order not found.")

    response = await _payos_request("GET", f"/v2/payment-requests/{order_code}")
    data = response.get("data") or {}
    status_value = str(data.get("status") or "").upper()

    if response.get("signature") and not _verify_payos_signature(data, str(response["signature"])):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="payOS response signature is invalid.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_billing_schema(cur)
            locked = _lock_payment_by_order_code(cur, order_code)
            if not locked or str(locked["user_id"]) != str(payment["user_id"]):
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Payment order not found.")

            if status_value == PAYOS_PAID_STATUS:
                result = _apply_paid_payment(cur, locked, data, response)
            elif status_value == PAYOS_CANCELLED_STATUS:
                result = _mark_locked_payment_status(cur, locked, PAYMENT_STATUS_CANCELLED, data, response)
            elif status_value == PAYOS_EXPIRED_STATUS:
                result = _mark_locked_payment_status(cur, locked, PAYMENT_STATUS_EXPIRED, data, response)
            else:
                result = _payment_payload(locked)
        conn.commit()

    return result


def handle_payos_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_billing_schema()
    _require_payos_checksum_key()

    signature = payload.get("signature")
    data = payload.get("data")
    if not isinstance(signature, str) or not isinstance(data, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid payOS webhook payload.")

    if not _verify_payos_signature(data, signature):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid payOS webhook signature.")

    order_code = _int_or_none(data.get("orderCode"))
    if not order_code:
        return {"received": True, "ignored": True, "reason": "missing_order_code"}

    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_billing_schema(cur)
            payment = _lock_payment_by_order_code(cur, order_code)
            if not payment:
                conn.commit()
                return {"received": True, "ignored": True, "reason": "unknown_order_code"}

            successful = bool(payload.get("success")) and str(data.get("code") or "") == "00"
            if successful:
                result = _apply_paid_payment(cur, payment, data, payload)
            else:
                result = _mark_locked_payment_status(cur, payment, PAYMENT_STATUS_FAILED, data, payload)
        conn.commit()

    return {"received": True, "payment": result}


def expire_due_subscriptions_sync() -> int:
    ensure_billing_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            count = _expire_due_subscriptions(cur)
        conn.commit()
    return count


def ensure_billing_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return

    ensure_plan_quota_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            _ensure_billing_schema(cur)
        conn.commit()
    _schema_ready = True


def _ensure_billing_schema(cur) -> None:
    cur.execute("alter table user_subscriptions add column if not exists payment_id bigint null")
    cur.execute("alter table user_subscriptions add column if not exists provider varchar(40) null")
    cur.execute("alter table user_subscriptions add column if not exists provider_subscription_id varchar(255) null")
    cur.execute("alter table user_subscriptions add column if not exists current_period_start timestamptz null")
    cur.execute("alter table user_subscriptions add column if not exists current_period_end timestamptz null")
    cur.execute("alter table user_subscriptions add column if not exists cancelled_at timestamptz null")
    cur.execute("alter table user_subscriptions add column if not exists updated_at timestamptz not null default now()")
    cur.execute(
        """
        create table if not exists payment_transactions (
          id bigserial primary key,
          user_id uuid not null references users(id) on delete cascade,
          plan_id bigint not null references plans(id),
          subscription_id bigint null references user_subscriptions(id) on delete set null,
          provider varchar(40) not null default 'payos',
          order_code bigint unique,
          payment_link_id varchar(255) null,
          checkout_url text null,
          amount_vnd int not null,
          currency varchar(10) not null default 'VND',
          status varchar(20) not null check (status in ('pending', 'paid', 'cancelled', 'expired', 'failed')),
          description varchar(255) null,
          period_start timestamptz null,
          period_end timestamptz null,
          paid_at timestamptz null,
          cancelled_at timestamptz null,
          expired_at timestamptz null,
          raw_request_json jsonb null,
          raw_response_json jsonb null,
          webhook_json jsonb null,
          created_at timestamptz not null default now(),
          updated_at timestamptz not null default now()
        )
        """
    )
    cur.execute("create index if not exists idx_payment_transactions_user_created on payment_transactions (user_id, created_at desc)")
    cur.execute("create index if not exists idx_payment_transactions_status_created on payment_transactions (status, created_at desc)")
    cur.execute("create index if not exists idx_payment_transactions_payment_link on payment_transactions (payment_link_id)")
    cur.execute(
        """
        do $$
        begin
          if not exists (
            select 1 from information_schema.table_constraints
            where constraint_name = 'fk_user_subscriptions_payment_id'
              and table_name = 'user_subscriptions'
          ) then
            alter table user_subscriptions
            add constraint fk_user_subscriptions_payment_id
            foreign key (payment_id) references payment_transactions(id) on delete set null;
          end if;
        end $$;
        """
    )


def _create_pending_payment(*, external_auth_id: str, email: str | None, plan_code: str) -> dict[str, Any]:
    ensure_billing_schema()
    _ensure_dev_billing_user(external_auth_id=external_auth_id, email=email)

    with get_connection() as conn:
        with conn.cursor() as cur:
            _expire_due_subscriptions(cur)
            user = _lookup_user(cur, external_auth_id=external_auth_id, email=email, for_update=True)
            if not user:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User profile has not been synced yet.")

            plan = _get_plan(cur, plan_code)
            amount_vnd = _price_to_vnd(plan["monthly_price"])
            if amount_vnd <= 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="This plan does not require payment.")

            cur.execute(
                """
                insert into payment_transactions (
                  user_id,
                  plan_id,
                  provider,
                  amount_vnd,
                  currency,
                  status
                )
                values (%s, %s, %s, %s, 'VND', %s)
                returning *
                """,
                (user["id"], plan["id"], PAYMENT_PROVIDER_PAYOS, amount_vnd, PAYMENT_STATUS_PENDING),
            )
            payment = cur.fetchone()
            order_code = _order_code_for_payment(payment["id"])
            description = _payment_description(payment["id"])
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
            cur.execute(
                """
                update payment_transactions
                set order_code = %s,
                    description = %s,
                    expired_at = %s,
                    updated_at = now()
                where id = %s
                returning *
                """,
                (order_code, description, expires_at, payment["id"]),
            )
            payment = cur.fetchone()
        conn.commit()

    payload = _payment_payload(payment)
    payload["plan_code"] = plan["code"]
    payload["plan_name"] = plan["name"]
    payload["buyer_email"] = user["email"]
    payload["buyer_name"] = user["full_name"]
    return payload


def _ensure_dev_billing_user(*, external_auth_id: str, email: str | None) -> None:
    if APP_ENV not in {"development", "dev", "test"} or AUTH_MODE != "dev":
        return
    if external_auth_id != DEV_USER_ID:
        return

    upsert_auth_user(
        external_auth_id=DEV_USER_ID,
        email=email or DEV_USER_EMAIL,
        full_name=DEV_USER_NAME,
        auth_provider="dev",
        identity_verified=True,
    )


def _payos_create_payment_payload(payment: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "orderCode": int(payment["order_code"]),
        "amount": int(payment["amount_vnd"]),
        "description": payment["description"],
        "buyerName": payment.get("buyer_name") or "",
        "buyerEmail": payment.get("buyer_email") or "",
        "items": [
            {
                "name": f"VieCareer {payment['plan_name']}",
                "quantity": 1,
                "price": int(payment["amount_vnd"]),
            }
        ],
        "cancelUrl": PAYOS_CANCEL_URL,
        "returnUrl": PAYOS_RETURN_URL,
        "expiredAt": int(payment["expires_at"].timestamp()) if payment.get("expires_at") else None,
    }
    signature_payload = {
        "amount": payload["amount"],
        "cancelUrl": payload["cancelUrl"],
        "description": payload["description"],
        "orderCode": payload["orderCode"],
        "returnUrl": payload["returnUrl"],
    }
    payload["signature"] = _payos_signature(signature_payload)
    return {key: value for key, value in payload.items() if value not in (None, "")}


async def _payos_request(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    _require_payos_config()
    headers = {
        "x-client-id": PAYOS_CLIENT_ID or "",
        "x-api-key": PAYOS_API_KEY or "",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(method, f"{PAYOS_API_BASE}{path}", headers=headers, json=json_body)
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="payOS returned a non-JSON response.") from exc
    if response.status_code >= 400:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=payload.get("desc") or "payOS request failed.")
    return payload


def _attach_payos_checkout_data(
    payment_id: int,
    request_payload: dict[str, Any],
    payos_response: dict[str, Any],
) -> dict[str, Any]:
    data = payos_response.get("data") or {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update payment_transactions
                set payment_link_id = %s,
                    checkout_url = %s,
                    raw_request_json = %s,
                    raw_response_json = %s,
                    updated_at = now()
                where id = %s
                returning *
                """,
                (
                    data.get("paymentLinkId"),
                    data.get("checkoutUrl"),
                    Json(request_payload),
                    Json(payos_response),
                    payment_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return _payment_payload(row)


def _mark_payment_failed(payment_id: int, raw_response: dict[str, Any]) -> None:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update payment_transactions
                    set status = %s,
                        raw_response_json = %s,
                        updated_at = now()
                    where id = %s
                    """,
                    (PAYMENT_STATUS_FAILED, Json(raw_response), payment_id),
                )
            conn.commit()
    except Exception:
        logger.exception("Failed to mark payment=%s as failed", payment_id)


def _apply_paid_payment(
    cur,
    payment: dict[str, Any],
    provider_data: dict[str, Any],
    raw_payload: dict[str, Any],
) -> dict[str, Any]:
    if payment["status"] == PAYMENT_STATUS_PAID and payment.get("subscription_id"):
        return _payment_payload(payment)

    paid_amount = _int_or_none(provider_data.get("amount")) or _int_or_none(provider_data.get("amountPaid"))
    if paid_amount is not None and paid_amount != int(payment["amount_vnd"]):
        return _mark_locked_payment_status(cur, payment, PAYMENT_STATUS_FAILED, provider_data, raw_payload)

    now = datetime.now(timezone.utc)
    plan = _get_plan_by_id(cur, payment["plan_id"])
    period_start, period_end, subscription_id = _activate_subscription_for_payment(cur, payment, plan, now)

    cur.execute(
        """
        update payment_transactions
        set status = %s,
            subscription_id = %s,
            period_start = %s,
            period_end = %s,
            paid_at = coalesce(paid_at, %s),
            payment_link_id = coalesce(payment_link_id, %s),
            webhook_json = %s,
            raw_response_json = coalesce(raw_response_json, %s),
            updated_at = now()
        where id = %s
        returning *
        """,
        (
            PAYMENT_STATUS_PAID,
            subscription_id,
            period_start,
            period_end,
            _parse_payos_datetime(provider_data.get("transactionDateTime")) or now,
            provider_data.get("paymentLinkId"),
            Json(raw_payload),
            Json(raw_payload),
            payment["id"],
        ),
    )
    return _payment_payload(cur.fetchone())


def _activate_subscription_for_payment(
    cur,
    payment: dict[str, Any],
    plan: dict[str, Any],
    now: datetime,
) -> tuple[datetime, datetime, int]:
    plan_code = str(plan["code"])
    if plan_code == "usage_based":
        period_start = now
        period_end = now + timedelta(days=30)
        cur.execute(
            """
            insert into user_subscriptions (
              user_id,
              plan_id,
              status,
              start_at,
              end_at,
              auto_renew,
              payment_id,
              provider,
              current_period_start,
              current_period_end,
              updated_at
            )
            values (%s, %s, 'active', %s, %s, false, %s, %s, %s, %s, now())
            returning id
            """,
            (
                payment["user_id"],
                plan["id"],
                period_start,
                period_end,
                payment["id"],
                PAYMENT_PROVIDER_PAYOS,
                period_start,
                period_end,
            ),
        )
        return period_start, period_end, int(cur.fetchone()["id"])

    current = _current_recurring_subscription(cur, payment["user_id"])
    current_priority = PLAN_PRIORITY.get(current["plan_code"], 0) if current else 0
    target_priority = PLAN_PRIORITY.get(plan_code, 0)

    latest_same_plan = _latest_active_subscription_for_plan(cur, payment["user_id"], plan_code)
    if latest_same_plan:
        period_start = max(now, _as_aware_datetime(latest_same_plan["end_at"]) or now)
        period_end = _add_month(period_start)
        cur.execute(
            """
            insert into user_subscriptions (
              user_id,
              plan_id,
              status,
              start_at,
              end_at,
              auto_renew,
              payment_id,
              provider,
              current_period_start,
              current_period_end,
              updated_at
            )
            values (%s, %s, 'active', %s, %s, false, %s, %s, %s, %s, now())
            returning id
            """,
            (
                payment["user_id"],
                plan["id"],
                period_start,
                period_end,
                payment["id"],
                PAYMENT_PROVIDER_PAYOS,
                period_start,
                period_end,
            ),
        )
        return period_start, period_end, int(cur.fetchone()["id"])

    if current and target_priority > current_priority:
        cur.execute(
            """
            update user_subscriptions
            set status = 'cancelled',
                end_at = %s,
                cancelled_at = %s,
                updated_at = now()
            where id = %s
            """,
            (now, now, current["id"]),
        )
        period_start = now
    elif current:
        period_start = _as_aware_datetime(current["end_at"]) or now
    else:
        period_start = now

    period_end = _add_month(period_start)
    cur.execute(
        """
        insert into user_subscriptions (
          user_id,
          plan_id,
          status,
          start_at,
          end_at,
          auto_renew,
          payment_id,
          provider,
          current_period_start,
          current_period_end,
          updated_at
        )
        values (%s, %s, 'active', %s, %s, false, %s, %s, %s, %s, now())
        returning id
        """,
        (
            payment["user_id"],
            plan["id"],
            period_start,
            period_end,
            payment["id"],
            PAYMENT_PROVIDER_PAYOS,
            period_start,
            period_end,
        ),
    )
    return period_start, period_end, int(cur.fetchone()["id"])


def _mark_locked_payment_status(
    cur,
    payment: dict[str, Any],
    next_status: str,
    provider_data: dict[str, Any],
    raw_payload: dict[str, Any],
) -> dict[str, Any]:
    # A verified successful payment is terminal. Delayed, duplicated, or
    # out-of-order failure/cancellation webhooks must never downgrade it.
    if payment["status"] == PAYMENT_STATUS_PAID:
        return _payment_payload(payment)

    timestamp_column = {
        PAYMENT_STATUS_CANCELLED: "cancelled_at",
        PAYMENT_STATUS_EXPIRED: "expired_at",
    }.get(next_status)
    timestamp_sql = f", {timestamp_column} = coalesce({timestamp_column}, now())" if timestamp_column else ""
    cur.execute(
        f"""
        update payment_transactions
        set status = %s,
            payment_link_id = coalesce(payment_link_id, %s),
            webhook_json = %s,
            raw_response_json = coalesce(raw_response_json, %s),
            updated_at = now()
            {timestamp_sql}
        where id = %s
        returning *
        """,
        (
            next_status,
            provider_data.get("paymentLinkId"),
            Json(raw_payload),
            Json(raw_payload),
            payment["id"],
        ),
    )
    return _payment_payload(cur.fetchone())


def _expire_due_subscriptions(cur) -> int:
    cur.execute(
        """
        update user_subscriptions
        set status = 'expired',
            updated_at = now()
        where status = 'active'
          and end_at is not null
          and end_at <= now()
        """
    )
    return cur.rowcount or 0


def _payment_for_authenticated_user(
    *,
    external_auth_id: str,
    email: str | None,
    order_code: int,
) -> dict[str, Any] | None:
    ensure_billing_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            user = _lookup_user(cur, external_auth_id=external_auth_id, email=email)
            if not user:
                return None
            cur.execute(
                """
                select p.*
                from payment_transactions p
                where p.user_id = %s
                  and p.order_code = %s
                limit 1
                """,
                (user["id"], order_code),
            )
            return cur.fetchone()


def _lock_payment_by_order_code(cur, order_code: int) -> dict[str, Any] | None:
    cur.execute(
        """
        select *
        from payment_transactions
        where order_code = %s
        for update
        """,
        (order_code,),
    )
    return cur.fetchone()


def _lookup_user(
    cur,
    *,
    external_auth_id: str,
    email: str | None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    lock_clause = "for update" if for_update else ""
    cur.execute(
        f"""
        select id, external_auth_id, email, full_name
        from users
        where external_auth_id = %s
        order by case when external_auth_id = %s then 0 else 1 end
        limit 1
        {lock_clause}
        """,
        (external_auth_id, external_auth_id),
    )
    return cur.fetchone()


def _get_plan(cur, plan_code: str) -> dict[str, Any]:
    cur.execute(
        """
        select id, code, name, monthly_price, billing_period, included_members
        from plans
        where code = %s
        limit 1
        """,
        (plan_code,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plan not found.")
    return row


def _get_plan_by_id(cur, plan_id: int) -> dict[str, Any]:
    cur.execute(
        """
        select id, code, name, monthly_price, billing_period, included_members
        from plans
        where id = %s
        limit 1
        """,
        (plan_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plan not found.")
    return row


def _list_plans(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        select
          p.id,
          p.code,
          p.name,
          p.monthly_price,
          p.billing_period,
          p.included_members,
          p.quota_total,
          p.rate_limit_per_minute
        from plans p
        order by
          case p.code
            when 'free' then 0
            when 'usage_based' then 1
            when 'standard' then 2
            when 'pro' then 3
            when 'team' then 4
            else 99
          end
        """
    )
    plans = [_plan_payload(row) for row in cur.fetchall()]
    for plan in plans:
        cur.execute(
            """
            select feature_code, metric, period, limit_value
            from plan_feature_limits
            where plan_id = %s
            order by feature_code, metric, period
            """,
            (plan["id"],),
        )
        plan["limits"] = [dict(row) for row in cur.fetchall()]
    return plans


def _current_subscription(cur, user_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        select
          s.*,
          p.code as plan_code,
          p.name as plan_name,
          p.monthly_price,
          p.billing_period,
          p.included_members
        from user_subscriptions s
        join plans p on p.id = s.plan_id
        where s.user_id = %s
          and s.status = 'active'
          and s.start_at <= now()
          and (s.end_at is null or s.end_at > now())
          and p.code <> 'usage_based'
        order by
          case p.code
            when 'team' then 400
            when 'pro' then 300
            when 'standard' then 200
            when 'free' then 100
            else 0
          end desc,
          s.start_at desc,
          s.id desc
        limit 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    return _subscription_payload(row) if row else None


def _current_recurring_subscription(cur, user_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        select
          s.*,
          p.code as plan_code
        from user_subscriptions s
        join plans p on p.id = s.plan_id
        where s.user_id = %s
          and s.status = 'active'
          and s.start_at <= now()
          and (s.end_at is null or s.end_at > now())
          and p.code = any(%s)
        order by
          case p.code
            when 'team' then 400
            when 'pro' then 300
            when 'standard' then 200
            else 0
          end desc,
          s.start_at desc,
          s.id desc
        limit 1
        """,
        (user_id, list(RECURRING_PLAN_CODES)),
    )
    return cur.fetchone()


def _latest_active_subscription_for_plan(cur, user_id: str, plan_code: str) -> dict[str, Any] | None:
    cur.execute(
        """
        select
          s.*,
          p.code as plan_code
        from user_subscriptions s
        join plans p on p.id = s.plan_id
        where s.user_id = %s
          and s.status = 'active'
          and (s.end_at is null or s.end_at > now())
          and p.code = %s
        order by s.end_at desc nulls last, s.start_at desc, s.id desc
        limit 1
        """,
        (user_id, plan_code),
    )
    return cur.fetchone()


def _payment_history_for_user(cur, user_id: str, *, limit: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        select
          pt.*,
          p.code as plan_code,
          p.name as plan_name,
          p.billing_period
        from payment_transactions pt
        join plans p on p.id = pt.plan_id
        where pt.user_id = %s
        order by pt.created_at desc
        limit %s
        """,
        (user_id, limit),
    )
    return [_payment_payload(row) for row in cur.fetchall()]


def _subscription_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "current_period_start": row.get("current_period_start") or row["start_at"],
        "current_period_end": row.get("current_period_end") or row["end_at"],
        "auto_renew": bool(row["auto_renew"]),
        "provider": row.get("provider"),
        "payment_id": row.get("payment_id"),
        "plan": {
            "id": row["plan_id"],
            "code": row["plan_code"],
            "name": row["plan_name"],
            "price_vnd": _price_to_vnd(row["monthly_price"]),
            "billing_period": row["billing_period"],
            "included_members": row["included_members"],
        },
    }


def _payment_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": str(row["user_id"]),
        "plan_id": row["plan_id"],
        "plan_code": row.get("plan_code"),
        "plan_name": row.get("plan_name"),
        "provider": row["provider"],
        "order_code": row["order_code"],
        "payment_link_id": row.get("payment_link_id"),
        "checkout_url": row.get("checkout_url"),
        "amount_vnd": int(row["amount_vnd"]),
        "currency": row["currency"],
        "status": row["status"],
        "description": row.get("description"),
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "paid_at": row.get("paid_at"),
        "cancelled_at": row.get("cancelled_at"),
        "expires_at": row.get("expired_at"),
        "expired_at": row.get("expired_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "subscription_id": row.get("subscription_id"),
    }


def _plan_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "price_vnd": _price_to_vnd(row["monthly_price"]),
        "billing_period": row["billing_period"],
        "included_members": row["included_members"],
        "quota_total": row.get("quota_total"),
        "rate_limit_per_minute": row.get("rate_limit_per_minute"),
    }


def _payos_signature(data: dict[str, Any]) -> str:
    _require_payos_checksum_key()
    query = _payos_query_string(data)
    return hmac.new(
        str(PAYOS_CHECKSUM_KEY).encode("utf-8"),
        msg=query.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def _verify_payos_signature(data: dict[str, Any], signature: str) -> bool:
    expected = _payos_signature(data)
    return hmac.compare_digest(expected, signature)


def _payos_query_string(data: dict[str, Any]) -> str:
    items = []
    for key in sorted(data):
        value = data[key]
        if value is None or value in ("undefined", "null", "NULL"):
            value_as_string = ""
        elif isinstance(value, bool):
            value_as_string = str(value)
        elif isinstance(value, (int, float, Decimal)):
            value_as_string = str(value)
        elif isinstance(value, dict):
            value_as_string = json.dumps(_sort_dict(value), ensure_ascii=False, separators=(",", ":")).replace(
                "None", "null"
            )
        elif isinstance(value, list):
            value_as_string = json.dumps(
                [_sort_dict(item) if isinstance(item, dict) else item for item in value],
                ensure_ascii=False,
                separators=(",", ":"),
            ).replace("None", "null")
        else:
            value_as_string = str(value)
        items.append(f"{key}={value_as_string}")
    return "&".join(items)


def _sort_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in sorted(value)}


def _require_payos_config() -> None:
    missing = [
        name
        for name, value in (
            ("PAYOS_CLIENT_ID", PAYOS_CLIENT_ID),
            ("PAYOS_API_KEY", PAYOS_API_KEY),
            ("PAYOS_CHECKSUM_KEY", PAYOS_CHECKSUM_KEY),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"payOS is not configured. Missing: {', '.join(missing)}.",
        )


def _require_payos_checksum_key() -> None:
    if not PAYOS_CHECKSUM_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="payOS checksum key is not configured.")


def _normalize_plan_code(plan_code: str) -> str:
    return plan_code.strip().lower().replace("-", "_")


def _price_to_vnd(value: Any) -> int:
    if isinstance(value, Decimal):
        return int(value)
    return int(Decimal(str(value or 0)))


def _order_code_for_payment(payment_id: int) -> int:
    today = datetime.now(timezone.utc).strftime("%y%m%d")
    return int(f"{today}{int(payment_id):06d}")


def _payment_description(payment_id: int) -> str:
    return f"VC{int(payment_id) % 10000000:07d}"


def _add_month(value: datetime) -> datetime:
    value = _as_aware_datetime(value) or datetime.now(timezone.utc)
    month = value.month + 1
    year = value.year
    if month > 12:
        month = 1
        year += 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _as_aware_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_payos_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _as_aware_datetime(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(str(value), fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
