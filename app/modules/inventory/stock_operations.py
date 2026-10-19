
"""Transactional stock consumption with FIFO/LIFO and safe retries."""

import json
import random
import time
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.modules.inventory.models import (
    StockBatch,
    StockConsumptionRecord,
    StockMovement,
)
from app.modules.inventory.valuation import (
    AllocationResult,
    BatchAllocation,
    InventoryValuationEngine,
    ValuationMethod,
)


class StockConsumptionService:
    """Consume stock atomically with idempotency and bounded lock retries."""

    MAX_RETRIES = 5
    INITIAL_RETRY_DELAY = 0.02
    MAX_RETRY_DELAY = 0.25

    def __init__(self, db: Session):
        self.db = db
        self.valuation = InventoryValuationEngine(db)

    @staticmethod
    def _method_value(method: ValuationMethod | str) -> str:
        try:
            return ValuationMethod(method).value
        except (ValueError, TypeError):
            raise ValidationError("Valuation method must be FIFO or LIFO.")

    @staticmethod
    def _is_transient_lock_error(exc: OperationalError) -> bool:
        message = str(exc).lower()
        return "locked" in message or "busy" in message

    @staticmethod
    def _request_matches(
        record: StockConsumptionRecord,
        branch_id: str,
        product_id: str,
        quantity: int,
        method: str,
    ) -> bool:
        return (
            record.branch_id == branch_id
            and record.product_id == product_id
            and record.quantity == quantity
            and record.method == method
        )

    @staticmethod
    def _restore_result(record: StockConsumptionRecord) -> AllocationResult:
        """Reconstruct the original allocation result from saved JSON."""
        data = json.loads(record.allocations_json)

        allocations = tuple(
            BatchAllocation(
                batch_id=item["batch_id"],
                batch_number=item["batch_number"],
                quantity=item["quantity"],
                unit_cost=Decimal(item["unit_cost"]),
                total_cost=Decimal(item["total_cost"]),
            )
            for item in data
        )

        return AllocationResult(
            branch_id=record.branch_id,
            product_id=record.product_id,
            requested_quantity=record.quantity,
            method=ValuationMethod(record.method),
            allocations=allocations,
            total_cost=Decimal(record.total_cost),
        )

    def _replay_existing(
        self,
        idempotency_key: str,
        branch_id: str,
        product_id: str,
        quantity: int,
        method: str,
    ) -> AllocationResult | None:
        record = (
            self.db.query(StockConsumptionRecord)
            .filter_by(idempotency_key=idempotency_key)
            .one_or_none()
        )

        if record is None:
            return None

        if not self._request_matches(
            record,
            branch_id,
            product_id,
            quantity,
            method,
        ):
            raise ValidationError(
                "Idempotency key has already been used with different "
                "checkout details."
            )

        return self._restore_result(record)

    def consume(
        self,
        branch_id: str,
        product_id: str,
        quantity: int,
        method: ValuationMethod | str = ValuationMethod.FIFO,
        *,
        reference_id: str | None = None,
        notes: str | None = None,
        idempotency_key: str | None = None,
    ) -> AllocationResult:
        """Consume stock with atomic updates and optional idempotency.

        Transient SQLite lock errors trigger bounded retries of the complete
        transaction. Validation errors and other database errors are not
        retried.

        Each attempt uses the same idempotency key so an already committed
        checkout can be replayed instead of deducted twice.
        """
        if idempotency_key is not None:
            if not idempotency_key.strip():
                raise ValidationError("Idempotency key cannot be empty.")
            if len(idempotency_key) > 128:
                raise ValidationError(
                    "Idempotency key cannot exceed 128 characters."
                )

        method_value = self._method_value(method)

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                return self._consume_once(
                    branch_id=branch_id,
                    product_id=product_id,
                    quantity=quantity,
                    method_value=method_value,
                    reference_id=reference_id,
                    notes=notes,
                    idempotency_key=idempotency_key,
                )

            except OperationalError as exc:
                self.db.rollback()

                if (
                    not self._is_transient_lock_error(exc)
                    or attempt >= self.MAX_RETRIES
                ):
                    raise

                # Exponential backoff with jitter reduces synchronized retries.
                delay = min(
                    self.INITIAL_RETRY_DELAY * (2**attempt),
                    self.MAX_RETRY_DELAY,
                )
                time.sleep(delay + random.uniform(0, delay * 0.25))

        raise RuntimeError("Stock consumption retry loop ended unexpectedly.")

    def _consume_once(
        self,
        *,
        branch_id: str,
        product_id: str,
        quantity: int,
        method_value: str,
        reference_id: str | None,
        notes: str | None,
        idempotency_key: str | None,
    ) -> AllocationResult:
        """Execute one complete transaction, rolling back on every failure."""
        try:
            if idempotency_key is not None:
                existing = self._replay_existing(
                    idempotency_key,
                    branch_id,
                    product_id,
                    quantity,
                    method_value,
                )
                if existing is not None:
                    return existing

            plan = self.valuation.allocate(
                branch_id=branch_id,
                product_id=product_id,
                quantity=quantity,
                method=method_value,
            )

            if idempotency_key is not None:
                serialized_allocations = [
                    {
                        "batch_id": allocation.batch_id,
                        "batch_number": allocation.batch_number,
                        "quantity": allocation.quantity,
                        "unit_cost": str(allocation.unit_cost),
                        "total_cost": str(allocation.total_cost),
                    }
                    for allocation in plan.allocations
                ]

                self.db.add(
                    StockConsumptionRecord(
                        id=str(uuid4()),
                        idempotency_key=idempotency_key,
                        branch_id=branch_id,
                        product_id=product_id,
                        quantity=quantity,
                        method=method_value,
                        total_cost=str(plan.total_cost),
                        allocations_json=json.dumps(serialized_allocations),
                    )
                )

                # Claim the unique key before making stock deductions.
                self.db.flush()

            for allocation in plan.allocations:
                result = self.db.execute(
                    update(StockBatch)
                    .where(
                        StockBatch.id == allocation.batch_id,
                        StockBatch.branch_id == branch_id,
                        StockBatch.product_id == product_id,
                        StockBatch.quantity >= allocation.quantity,
                    )
                    .values(
                        quantity=StockBatch.quantity - allocation.quantity
                    )
                    .execution_options(synchronize_session=False)
                )

                if result.rowcount != 1:
                    raise ValidationError(
                        "Stock changed during consumption. Retry the operation."
                    )

                self.db.add(
                    StockMovement(
                        id=str(uuid4()),
                        branch_id=branch_id,
                        batch_id=allocation.batch_id,
                        product_id=product_id,
                        movement_type="SALE",
                        quantity_delta=-allocation.quantity,
                        unit_cost=float(allocation.unit_cost),
                        reference_type=(
                            "SALE"
                            if reference_id is not None
                            or idempotency_key is not None
                            else None
                        ),
                        reference_id=(
                            reference_id
                            if reference_id is not None
                            else idempotency_key
                        ),
                        notes=notes,
                    )
                )

            self.db.commit()
            return plan

        except IntegrityError:
            self.db.rollback()

            # A competing request may have committed the same key first.
            if idempotency_key is not None:
                try:
                    existing = self._replay_existing(
                        idempotency_key,
                        branch_id,
                        product_id,
                        quantity,
                        method_value,
                    )
                    if existing is not None:
                        return existing
                except Exception:
                    self.db.rollback()
                    raise

            raise

        except Exception:
            self.db.rollback()
            raise
