
"""Concurrency tests for competing inventory checkout requests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ValidationError
from app.db.base import Base
from app.modules.inventory.models import (
    Branch,
    StockBatch,
    StockMovement,
    StockConsumptionRecord,
)
from app.modules.inventory.stock_operations import StockConsumptionService


def test_concurrent_checkouts_never_oversell_stock(tmp_path):
    """Competing sessions must never deduct more stock than is available."""

    database_path = tmp_path / "concurrency_test.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    # Ensure all ORM models have registered their tables.
    Base.metadata.create_all(bind=engine)

    TestSession = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

    available_stock = 10
    checkout_count = 500
    worker_count = 50

    # Seed inventory before starting concurrent requests.
    with TestSession() as session:
        session.add(
            Branch(
                id="concurrency-branch",
                name="Concurrency Warehouse",
                code="CONC-01",
                is_warehouse=True,
            )
        )
        session.flush()

        session.add(
            StockBatch(
                id="concurrency-batch",
                branch_id="concurrency-branch",
                product_id="concurrency-product",
                batch_number="CONC-BATCH-01",
                quantity=available_stock,
                purchase_price=10.0,
                selling_price=15.0,
            )
        )
        session.commit()

    start_barrier = Barrier(worker_count)

    def checkout(index):
        """Each worker owns its own database session."""
        with TestSession() as session:
            try:
                # Synchronize the first wave of independent sessions.
                if index < worker_count:
                    start_barrier.wait(timeout=30)

                result = StockConsumptionService(session).consume(
                    branch_id="concurrency-branch",
                    product_id="concurrency-product",
                    quantity=1,
                    reference_id=f"CONCURRENT-ORDER-{index}",
                    idempotency_key=f"concurrent-key-{uuid4()}",
                )

                return ("success", result.allocated_quantity)

            except ValidationError as exc:
                session.rollback()
                return ("rejected", str(exc))

            except OperationalError as exc:
                session.rollback()

                # SQLite may reject a competing transaction because of
                # lock contention. Record it rather than hiding it.
                message = str(exc).lower()
                if "locked" in message or "busy" in message:
                    return ("database_busy", str(exc))

                raise

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(
                executor.map(checkout, range(checkout_count))
            )

        successes = [
            outcome for outcome in outcomes
            if outcome[0] == "success"
        ]

        busy_failures = [
            outcome for outcome in outcomes
            if outcome[0] == "database_busy"
        ]

        rejected = [
            outcome for outcome in outcomes
            if outcome[0] == "rejected"
        ]

        # Inspect final committed database state with a fresh session.
        with TestSession() as session:
            batch = session.get(StockBatch, "concurrency-batch")
            remaining_stock = batch.quantity

            movements = (
                session.query(StockMovement)
                .filter_by(
                    branch_id="concurrency-branch",
                    product_id="concurrency-product",
                )
                .all()
            )

            consumption_records = (
                session.query(StockConsumptionRecord)
                .filter_by(branch_id="concurrency-branch")
                .all()
            )

            total_units_deducted = -sum(
                movement.quantity_delta for movement in movements
            )

            assert remaining_stock >= 0
            assert remaining_stock <= available_stock

            # Successful checkouts and committed inventory deductions
            # must agree exactly.
            assert len(successes) == len(movements)
            assert total_units_deducted == len(successes)
            assert len(consumption_records) == len(successes)

            # No checkout may sell more than the initial inventory.
            assert len(successes) <= available_stock

            # All requests must have a recorded outcome.
            assert (
                len(successes) + len(rejected) + len(busy_failures)
                == checkout_count
            )

            # Report useful diagnostics if SQLite contention limits
            # successful requests.
            print(
                f"\nConcurrent checkout results: "
                f"{len(successes)} succeeded, "
                f"{len(rejected)} rejected, "
                f"{len(busy_failures)} hit SQLite lock contention."
            )

    finally:
        engine.dispose()


def test_concurrent_duplicate_checkout_deducts_stock_once(tmp_path):
    """Concurrent retries with one idempotency key must deduct stock once."""

    database_path = tmp_path / "duplicate_checkout_test.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    Base.metadata.create_all(bind=engine)

    TestSession = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

    available_stock = 10
    request_count = 20
    shared_key = "same-checkout-idempotency-key"

    # Create the warehouse and its initial inventory.
    with TestSession() as session:
        session.add(
            Branch(
                id="duplicate-branch",
                name="Duplicate Test Warehouse",
                code="DUP-01",
                is_warehouse=True,
            )
        )
        session.flush()

        session.add(
            StockBatch(
                id="duplicate-batch",
                branch_id="duplicate-branch",
                product_id="duplicate-product",
                batch_number="DUP-BATCH-01",
                quantity=available_stock,
                purchase_price=10.0,
                selling_price=15.0,
            )
        )
        session.commit()

    start_barrier = Barrier(request_count)

    def submit_duplicate(_):
        with TestSession() as session:
            try:
                start_barrier.wait(timeout=30)

                result = StockConsumptionService(session).consume(
                    branch_id="duplicate-branch",
                    product_id="duplicate-product",
                    quantity=1,
                    reference_id="DUPLICATE-ORDER-001",
                    idempotency_key=shared_key,
                )
                return result
            finally:
                session.close()

    try:
        with ThreadPoolExecutor(max_workers=request_count) as executor:
            results = list(
                executor.map(submit_duplicate, range(request_count))
            )

        # All retries must return the same successful checkout result.
        assert len(results) == request_count
        assert all(result.allocated_quantity == 1 for result in results)
        assert all(result.total_cost == results[0].total_cost
                   for result in results)

        with TestSession() as session:
            batch = session.get(StockBatch, "duplicate-batch")

            movements = (
                session.query(StockMovement)
                .filter_by(
                    branch_id="duplicate-branch",
                    product_id="duplicate-product",
                )
                .all()
            )

            records = (
                session.query(StockConsumptionRecord)
                .filter_by(idempotency_key=shared_key)
                .all()
            )

            assert batch.quantity == available_stock - 1
            assert len(movements) == 1
            assert sum(m.quantity_delta for m in movements) == -1
            assert len(records) == 1

        print(
            f"\nDuplicate checkout results: {request_count} requests, "
            "1 stock deduction, 1 movement, 1 idempotency record."
        )

    finally:
        engine.dispose()
