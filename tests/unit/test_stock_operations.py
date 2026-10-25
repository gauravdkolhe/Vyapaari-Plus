
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.exceptions import ValidationError
from app.modules.inventory.models import (
    Branch,
    StockBatch,
    StockMovement,
    StockConsumptionRecord,
)
from app.modules.inventory.stock_operations import StockConsumptionService


def seed_stock(db_session):
    now = datetime.now(timezone.utc)

    db_session.add(
        Branch(
            id="branch-1",
            name="Main Warehouse",
            code="WH-01",
            is_warehouse=True,
        )
    )
    db_session.flush()

    db_session.add_all(
        [
            StockBatch(
                id="batch-1",
                branch_id="branch-1",
                product_id="product-1",
                batch_number="B001",
                quantity=5,
                purchase_price=10.0,
                selling_price=15.0,
                created_at=now - timedelta(days=5),
            ),
            StockBatch(
                id="batch-2",
                branch_id="branch-1",
                product_id="product-1",
                batch_number="B002",
                quantity=8,
                purchase_price=12.0,
                selling_price=18.0,
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    db_session.commit()


def get_quantities(db_session):
    return {
        batch.id: batch.quantity
        for batch in db_session.query(StockBatch).all()
    }


def test_consume_deducts_across_batches_atomically(db_session):
    seed_stock(db_session)

    result = StockConsumptionService(db_session).consume(
        "branch-1",
        "product-1",
        7,
        "FIFO",
        reference_id="ORDER-001",
    )

    assert get_quantities(db_session) == {
        "batch-1": 0,
        "batch-2": 6,
    }
    assert result.allocated_quantity == 7
    assert result.total_cost == Decimal("74.0")

    movements = (
        db_session.query(StockMovement)
        .order_by(StockMovement.batch_id)
        .all()
    )

    assert len(movements) == 2
    assert [m.batch_id for m in movements] == ["batch-1", "batch-2"]
    assert [m.quantity_delta for m in movements] == [-5, -2]
    assert all(m.movement_type == "SALE" for m in movements)
    assert all(m.branch_id == "branch-1" for m in movements)
    assert all(m.product_id == "product-1" for m in movements)
    assert all(m.reference_type == "SALE" for m in movements)
    assert all(m.reference_id == "ORDER-001" for m in movements)
    assert [m.unit_cost for m in movements] == [10.0, 12.0]


def test_consumption_without_reference_still_logs_movements(db_session):
    seed_stock(db_session)

    StockConsumptionService(db_session).consume(
        "branch-1",
        "product-1",
        2,
    )

    movements = db_session.query(StockMovement).all()

    assert len(movements) == 1
    assert movements[0].quantity_delta == -2
    assert movements[0].movement_type == "SALE"
    assert movements[0].reference_id is None
    assert movements[0].reference_type is None


def test_insufficient_stock_leaves_stock_and_ledger_unchanged(db_session):
    seed_stock(db_session)

    before = get_quantities(db_session)

    with pytest.raises(ValidationError, match="Insufficient eligible stock"):
        StockConsumptionService(db_session).consume(
            "branch-1",
            "product-1",
            14,
        )

    assert get_quantities(db_session) == before
    assert db_session.query(StockMovement).count() == 0


def test_second_batch_conflict_rolls_back_first_update(db_session):
    seed_stock(db_session)

    service = StockConsumptionService(db_session)
    original_allocate = service.valuation.allocate

    def stale_plan(*args, **kwargs):
        plan = original_allocate(*args, **kwargs)

        db_session.query(StockBatch).filter_by(id="batch-2").update(
            {"quantity": 1},
            synchronize_session=False,
        )
        db_session.flush()
        return plan

    service.valuation.allocate = stale_plan

    with pytest.raises(ValidationError, match="Stock changed"):
        service.consume("branch-1", "product-1", 7, "FIFO")

    db_session.expire_all()

    assert get_quantities(db_session) == {
        "batch-1": 5,
        "batch-2": 8,
    }
    assert db_session.query(StockMovement).count() == 0


def test_audit_insert_failure_rolls_back_stock_deduction(db_session):
    seed_stock(db_session)

    def reject_movement_insert(mapper, connection, target):
        raise RuntimeError("Simulated audit insert failure")

    event.listen(
        StockMovement,
        "before_insert",
        reject_movement_insert,
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="Simulated audit insert failure",
        ):
            StockConsumptionService(db_session).consume(
                "branch-1",
                "product-1",
                3,
            )
    finally:
        event.remove(
            StockMovement,
            "before_insert",
            reject_movement_insert,
        )

    db_session.expire_all()

    assert get_quantities(db_session) == {
        "batch-1": 5,
        "batch-2": 8,
    }
    assert db_session.query(StockMovement).count() == 0


def test_idempotent_retry_does_not_deduct_stock_twice(db_session):
    seed_stock(db_session)
    service = StockConsumptionService(db_session)

    first_result = service.consume(
        "branch-1",
        "product-1",
        3,
        "FIFO",
        reference_id="ORDER-IDEM-001",
        idempotency_key="checkout-key-001",
    )

    quantities_after_first = get_quantities(db_session)
    movement_count_after_first = db_session.query(StockMovement).count()

    retry_result = service.consume(
        "branch-1",
        "product-1",
        3,
        "FIFO",
        reference_id="ORDER-IDEM-001",
        idempotency_key="checkout-key-001",
    )

    assert get_quantities(db_session) == quantities_after_first
    assert db_session.query(StockMovement).count() == movement_count_after_first
    assert retry_result.total_cost == first_result.total_cost
    assert retry_result.allocated_quantity == first_result.allocated_quantity
    assert len(retry_result.allocations) == len(first_result.allocations)
    assert db_session.query(StockConsumptionRecord).count() == 1


def test_idempotency_key_reuse_with_different_quantity_is_rejected(db_session):
    seed_stock(db_session)
    service = StockConsumptionService(db_session)

    service.consume(
        "branch-1",
        "product-1",
        3,
        idempotency_key="checkout-key-002",
    )

    quantities_after_first = get_quantities(db_session)
    movement_count_after_first = db_session.query(StockMovement).count()

    with pytest.raises(ValidationError, match="different checkout details"):
        service.consume(
            "branch-1",
            "product-1",
            4,
            idempotency_key="checkout-key-002",
        )

    assert get_quantities(db_session) == quantities_after_first
    assert db_session.query(StockMovement).count() == movement_count_after_first
    assert db_session.query(StockConsumptionRecord).count() == 1


def test_idempotency_key_reuse_with_different_branch_is_rejected(db_session):
    seed_stock(db_session)
    service = StockConsumptionService(db_session)

    service.consume(
        "branch-1",
        "product-1",
        2,
        idempotency_key="checkout-key-003",
    )

    with pytest.raises(ValidationError, match="different checkout details"):
        service.consume(
            "branch-2",
            "product-1",
            2,
            idempotency_key="checkout-key-003",
        )

    assert db_session.query(StockConsumptionRecord).count() == 1


def test_failed_checkout_rolls_back_idempotency_record(db_session):
    seed_stock(db_session)

    def reject_movement_insert(mapper, connection, target):
        raise RuntimeError("Simulated audit insert failure")

    event.listen(
        StockMovement,
        "before_insert",
        reject_movement_insert,
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="Simulated audit insert failure",
        ):
            StockConsumptionService(db_session).consume(
                "branch-1",
                "product-1",
                3,
                idempotency_key="checkout-key-004",
            )
    finally:
        event.remove(
            StockMovement,
            "before_insert",
            reject_movement_insert,
        )

    db_session.expire_all()

    assert get_quantities(db_session) == {
        "batch-1": 5,
        "batch-2": 8,
    }
    assert db_session.query(StockMovement).count() == 0
    assert db_session.query(StockConsumptionRecord).count() == 0
