
"""Scheduled inventory monitoring jobs."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.modules.inventory.burn_rate import InventoryBurnRateService
from app.modules.inventory.models import ReorderPolicy
from app.modules.inventory.reorder_service import InventoryReorderService

logger = logging.getLogger(__name__)


def run_daily_inventory_check(
    db: Session | None = None,
    warning_days: int = 7,
    lookback_days: int = 30,
) -> dict:
    """
    Check low-stock policies and stockout forecasts.

    Pass an existing session for tests or reuse. When no session is passed,
    this function creates and closes its own session.
    """

    owns_session = db is None
    session = db if db is not None else SessionLocal()

    try:
        reorder_service = InventoryReorderService(session)
        burn_service = InventoryBurnRateService(session)

        low_stock_alerts = reorder_service.get_low_stock_alerts()

        # Create missing requisitions for products below their thresholds.
        requisitions = reorder_service.generate_low_stock_requisitions()

        policies = (
            session.query(ReorderPolicy)
            .order_by(ReorderPolicy.branch_id, ReorderPolicy.product_id)
            .all()
        )

        forecasts = []
        for policy in policies:
            forecast = burn_service.calculate(
                branch_id=policy.branch_id,
                product_id=policy.product_id,
                lookback_days=lookback_days,
            )

            days_remaining = forecast["days_until_stockout"]
            if (
                days_remaining is not None
                and days_remaining <= warning_days
            ):
                forecasts.append(forecast)

        result = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "low_stock_alert_count": len(low_stock_alerts),
            "requisition_count": len(requisitions),
            "stockout_warning_count": len(forecasts),
            "low_stock_alerts": low_stock_alerts,
            "requisitions": [
                {
                    "id": item.id,
                    "branch_id": item.branch_id,
                    "product_id": item.product_id,
                    "requested_quantity": item.requested_quantity,
                    "status": item.status,
                }
                for item in requisitions
            ],
            "stockout_warnings": forecasts,
        }

        logger.info(
            "Daily inventory check completed: %s",
            {
                "low_stock_alerts": result["low_stock_alert_count"],
                "requisitions": result["requisition_count"],
                "stockout_warnings": result["stockout_warning_count"],
            },
        )

        return result

    except Exception:
        logger.exception("Daily inventory check failed")
        raise

    finally:
        if owns_session:
            session.close()
