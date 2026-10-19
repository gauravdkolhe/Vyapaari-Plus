
"""FIFO/LIFO batch allocation and stock valuation.

Allocation is planned without mutating stock. The caller is responsible
for applying the allocation inside a database transaction.
"""

import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.modules.inventory.models import StockBatch


class ValuationMethod(str, enum.Enum):
    FIFO = "FIFO"
    LIFO = "LIFO"


@dataclass(frozen=True)
class BatchAllocation:
    batch_id: str
    batch_number: str
    quantity: int
    unit_cost: Decimal
    total_cost: Decimal


@dataclass(frozen=True)
class AllocationResult:
    branch_id: str
    product_id: str
    requested_quantity: int
    method: ValuationMethod
    allocations: tuple[BatchAllocation, ...]
    total_cost: Decimal

    @property
    def allocated_quantity(self) -> int:
        return sum(item.quantity for item in self.allocations)


def _utc(value: datetime) -> datetime:
    """Normalize SQLite's possibly-naive datetime values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class InventoryValuationEngine:
    """Plan FIFO/LIFO stock allocations across eligible batches."""

    def __init__(self, db: Session):
        self.db = db

    def allocate(
        self,
        branch_id: str,
        product_id: str,
        quantity: int,
        method: ValuationMethod | str = ValuationMethod.FIFO,
        as_of: datetime | None = None,
    ) -> AllocationResult:
        """Plan an allocation without changing database quantities.

        Expired and empty batches are excluded. If eligible stock is
        insufficient, raise ValidationError without modifying inventory.
        """
        if not isinstance(branch_id, str) or not branch_id.strip():
            raise ValidationError("Branch ID is required.")

        if not isinstance(product_id, str) or not product_id.strip():
            raise ValidationError("Product ID is required.")

        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise ValidationError("Quantity must be a positive integer.")

        try:
            method = ValuationMethod(method.upper())
        except (ValueError, AttributeError):
            raise ValidationError("Method must be FIFO or LIFO.") from None

        if as_of is not None and not isinstance(as_of, datetime):
            raise ValidationError("as_of must be a datetime.")

        now = _utc(as_of or datetime.now(timezone.utc))

        batches = (
            self.db.query(StockBatch)
            .filter(
                StockBatch.branch_id == branch_id,
                StockBatch.product_id == product_id,
                StockBatch.quantity > 0,
            )
            .all()
        )

        # Filter expiry in Python to handle SQLite's naive datetimes.
        eligible = [
            batch
            for batch in batches
            if batch.expiry_date is None
            or _utc(batch.expiry_date) >= now
        ]

        # Stable ordering when batches have the same creation timestamp.
        eligible.sort(
            key=lambda batch: (
                _utc(batch.created_at),
                batch.id,
            ),
            reverse=(method == ValuationMethod.LIFO),
        )

        available = sum(batch.quantity for batch in eligible)
        if available < quantity:
            raise ValidationError(
                f"Insufficient eligible stock for product {product_id}: "
                f"requested {quantity}, available {available}."
            )

        remaining = quantity
        allocations = []
        total_cost = Decimal("0")

        for batch in eligible:
            if remaining == 0:
                break

            taken = min(batch.quantity, remaining)
            unit_cost = Decimal(str(batch.purchase_price))
            line_cost = unit_cost * taken

            allocations.append(
                BatchAllocation(
                    batch_id=batch.id,
                    batch_number=batch.batch_number,
                    quantity=taken,
                    unit_cost=unit_cost,
                    total_cost=line_cost,
                )
            )

            total_cost += line_cost
            remaining -= taken

        return AllocationResult(
            branch_id=branch_id,
            product_id=product_id,
            requested_quantity=quantity,
            method=method,
            allocations=tuple(allocations),
            total_cost=total_cost,
        )
