
"""Inventory burn-rate and stockout forecasting."""

from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import func

from app.modules.inventory.models import StockMovement
from app.modules.inventory.reorder_service import InventoryReorderService


class InventoryBurnRateService:
    """Calculate sales-based daily consumption and stockout estimates."""

    def __init__(self, db):
        self.db = db
        self.reorder_service = InventoryReorderService(db)

    def calculate(
        self,
        branch_id: str,
        product_id: str,
        lookback_days: int = 30,
        as_of: datetime | None = None,
    ) -> dict:
        """
        Calculate average daily sales consumption.

        Only negative SALE movements are counted. Transfers, receipts,
        shrinkage, and other movement types are excluded.
        """

        if not branch_id or not branch_id.strip():
            raise ValueError("Branch ID is required.")

        if not product_id or not product_id.strip():
            raise ValueError("Product ID is required.")

        if lookback_days <= 0:
            raise ValueError("lookback_days must be greater than zero.")

        if as_of is None:
            as_of = datetime.now(timezone.utc)

        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)

        period_start = as_of - timedelta(days=lookback_days)

        consumed = (
            self.db.query(
                func.coalesce(func.sum(-StockMovement.quantity_delta), 0)
            )
            .filter(
                StockMovement.branch_id == branch_id,
                StockMovement.product_id == product_id,
                StockMovement.movement_type == "SALE",
                StockMovement.quantity_delta < 0,
                StockMovement.created_at >= period_start,
                StockMovement.created_at <= as_of,
            )
            .scalar()
        )

        total_consumed = int(consumed or 0)
        daily_burn_rate = total_consumed / lookback_days

        available_stock = self.reorder_service.get_available_stock(
            branch_id=branch_id,
            product_id=product_id,
            as_of=as_of.replace(tzinfo=timezone.utc),
        )

        if daily_burn_rate > 0:
            days_until_stockout = available_stock / daily_burn_rate
            estimated_stockout_date = as_of + timedelta(
                days=days_until_stockout
            )
            days_until_stockout_rounded = ceil(days_until_stockout)
            estimated_stockout_date = estimated_stockout_date.date().isoformat()
        else:
            days_until_stockout = None
            days_until_stockout_rounded = None
            estimated_stockout_date = None

        return {
            "branch_id": branch_id,
            "product_id": product_id,
            "period_days": lookback_days,
            "total_consumed": total_consumed,
            "average_daily_burn_rate": round(daily_burn_rate, 2),
            "available_stock": available_stock,
            "days_until_stockout": (
                round(days_until_stockout, 2)
                if days_until_stockout is not None
                else None
            ),
            "days_until_stockout_rounded": days_until_stockout_rounded,
            "estimated_stockout_date": estimated_stockout_date,
            "calculated_at": as_of.replace(tzinfo=timezone.utc).isoformat(),
        }

    def get_low_runway_products(
        self,
        branch_id: str,
        product_ids: list[str],
        warning_days: int = 7,
        lookback_days: int = 30,
    ) -> list[dict]:
        """Return products forecast to run out within warning_days."""

        if warning_days < 0:
            raise ValueError("warning_days cannot be negative.")

        results = []

        for product_id in product_ids:
            result = self.calculate(
                branch_id=branch_id,
                product_id=product_id,
                lookback_days=lookback_days,
            )

            days_remaining = result["days_until_stockout"]

            if days_remaining is not None and days_remaining <= warning_days:
                results.append(result)

        return results
