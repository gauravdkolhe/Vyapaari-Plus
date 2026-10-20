from datetime import datetime, timezone

import pytest

from app.core.exceptions import ValidationError
from app.modules.inventory.models import (
    Branch,
    CarrierLog,
    ReconciliationApproval,
    ReconciliationLog,
    StockBatch,
    StockTransfer,
    TransferStatus,
)
from app.modules.inventory.stock_transfer import StockTransferService
from app.modules.inventory.reconciliation import (
    MultiBranchReconciliationEngine,
)


def create_branches(db_session):
    warehouse = Branch(
        id="b1",
        name="Main Warehouse",
        code="WH-01",
        is_warehouse=True,
    )
    store = Branch(
        id="b2",
        name="Downtown Store",
        code="STR-02",
        is_warehouse=False,
    )

    db_session.add_all([warehouse, store])
    db_session.commit()

    source_batch = StockBatch(
        id="batch-b1-p1",
        product_id="p1",
        branch_id="b1",
        batch_number="BATCH-001",
        quantity=1000,
        purchase_price=100.0,
        selling_price=120.0,
    )

    db_session.add(source_batch)
    db_session.commit()


def test_stock_transfer_lifecycle(db_session):
    """Test transfer creation, dispatch, transit, receipt and reconciliation."""
    create_branches(db_session)

    service = StockTransferService(db_session)
    reconciler = MultiBranchReconciliationEngine(db_session)

    items = [
        {
            "product_id": "p1",
            "product_name": "Premium Basmati Rice 5kg",
            "quantity_dispatched": 20,
            "unit_cost": 450.0,
        }
    ]

    # 1. Create transfer request
    transfer = service.create_transfer_request(
        "b1",
        "b2",
        items,
        dispatched_by="Ramesh Manager",
    )

    assert transfer.status == TransferStatus.DRAFT
    assert len(transfer.items) == 1
    assert transfer.items[0].quantity_dispatched == 20

    # 2. Dispatch the transfer
    dispatched = service.dispatch_transfer(
        transfer.id,
        vehicle_number="DL-01-AB-1234",
    )

    assert dispatched.status == TransferStatus.DISPATCHED
    assert dispatched.vehicle_number == "DL-01-AB-1234"
    assert dispatched.delivery_challan_number.startswith("CHL-")
    assert dispatched.gate_pass_number.startswith("GATE-")

    # 3. Start transit
    in_transit = service.start_transit(transfer.id)

    assert in_transit.status == TransferStatus.IN_TRANSIT

    # 4. Receive 19 units; 1 unit is missing/damaged
    received_items = [
        {
            "item_id": transfer.items[0].id,
            "quantity_received": 19,
        }
    ]

    received = service.receive_and_record_delivery(
        transfer.id,
        received_items,
        received_by="Suresh Store",
    )

    assert received.status == TransferStatus.RECEIVED
    assert received.items[0].quantity_received == 19
    assert received.delivered_at is not None

    # 5. Reconcile the quantity variance
    rec_log = reconciler.reconcile_transfer(
        transfer.id,
        reconciled_by="Auditor Sharma",
        remarks="1 unit damaged in transit",
    )

    assert rec_log.variance_units == 1
    assert rec_log.total_transit_loss_value == 450.0
    assert transfer.status == TransferStatus.DISPUTED


def test_create_transfer_rejects_non_positive_quantity(db_session):
    """Reject zero quantity and avoid creating a transfer."""
    create_branches(db_session)

    service = StockTransferService(db_session)

    with pytest.raises(ValidationError):
        service.create_transfer_request(
            "b1",
            "b2",
            [
                {
                    "product_id": "p1",
                    "quantity_dispatched": 0,
                }
            ],
        )

    assert db_session.query(StockTransfer).count() == 0


def test_receive_rejects_quantity_above_dispatched(db_session):
    """Reject a receipt quantity greater than the dispatched quantity."""
    create_branches(db_session)

    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [
            {
                "product_id": "p1",
                "quantity_dispatched": 5,
            }
        ],
    )

    service.dispatch_transfer(transfer.id)
    service.start_transit(transfer.id)

    with pytest.raises(ValidationError):
        service.receive_and_record_delivery(
            transfer.id,
            [
                {
                    "item_id": transfer.items[0].id,
                    "quantity_received": 6,
                }
            ],
        )

    db_session.rollback()

    refreshed_transfer = (
        db_session.query(StockTransfer)
        .filter(StockTransfer.id == transfer.id)
        .first()
    )

    assert refreshed_transfer.status == TransferStatus.IN_TRANSIT


def test_cannot_start_transit_before_dispatch(db_session):
    """A draft transfer must not enter transit directly."""
    create_branches(db_session)

    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [
            {
                "product_id": "p1",
                "quantity_dispatched": 5,
            }
        ],
    )

    with pytest.raises(ValidationError):
        service.start_transit(transfer.id)

    assert transfer.status == TransferStatus.DRAFT


def test_dispatch_generates_delivery_documents(db_session):
    """Dispatch should generate a challan, gate pass and audit event."""
    create_branches(db_session)
    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [{"product_id": "p1", "quantity_dispatched": 5}],
    )

    dispatched = service.dispatch_transfer(
        transfer.id,
        vehicle_number="TN-32-AB-1234",
    )

    assert dispatched.status == TransferStatus.DISPATCHED
    assert dispatched.delivery_challan_number.startswith("CHL-")
    assert dispatched.gate_pass_number.startswith("GATE-")

    event_types = {
        log.event_type for log in dispatched.carrier_logs
    }
    assert "DISPATCHED" in event_types


def test_update_carrier_tracking(db_session):
    """Carrier, ETA and vehicle details should be updated."""
    create_branches(db_session)
    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [{"product_id": "p1", "quantity_dispatched": 5}],
    )

    service.dispatch_transfer(transfer.id)

    eta = datetime(
        2026, 10, 15, 12, 0, tzinfo=timezone.utc
    )

    updated = service.update_carrier_tracking(
        transfer.id,
        carrier_name="FastTrack Logistics",
        estimated_delivery_at=eta,
        vehicle_number="TN-32-AB-1234",
    )

    assert updated.carrier_name == "FastTrack Logistics"
    assert updated.vehicle_number == "TN-32-AB-1234"
    assert updated.estimated_delivery_at is not None


def test_record_and_get_carrier_events(db_session):
    """Carrier events should be saved and returned in tracking history."""
    create_branches(db_session)
    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [{"product_id": "p1", "quantity_dispatched": 5}],
    )

    service.dispatch_transfer(transfer.id)
    service.start_transit(transfer.id)

    service.record_carrier_event(
        transfer.id,
        event_type="LOCATION_UPDATE",
        location="Chennai",
        details="Shipment reached Chennai hub",
        logged_by="Transport Manager",
    )

    history = service.get_tracking_history(transfer.id)

    assert len(history) >= 1
    assert any(
        event.event_type == "LOCATION_UPDATE"
        and event.location == "Chennai"
        for event in history
    )


def test_carrier_event_rejected_for_draft_transfer(db_session):
    """Carrier events should not be recorded for a draft transfer."""
    create_branches(db_session)
    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [{"product_id": "p1", "quantity_dispatched": 5}],
    )

    with pytest.raises(ValidationError):
        service.record_carrier_event(
            transfer.id,
            event_type="LOCATION_UPDATE",
            location="Chennai",
        )

    assert transfer.status == TransferStatus.DRAFT


def test_damaged_and_missing_stock_reconciliation(db_session):
    """Damaged and missing units must be accounted for separately."""
    create_branches(db_session)

    service = StockTransferService(db_session)
    reconciler = MultiBranchReconciliationEngine(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "quantity_dispatched": 10,
                "unit_cost": 100.0,
            }
        ],
    )

    service.dispatch_transfer(transfer.id)
    service.start_transit(transfer.id)

    service.receive_and_record_delivery(
        transfer.id,
        [
            {
                "item_id": transfer.items[0].id,
                "quantity_received": 7,
                "quantity_damaged": 2,
            }
        ],
        received_by="Store Manager",
    )

    db_session.refresh(transfer)
    item = transfer.items[0]

    assert item.quantity_received == 7
    assert item.quantity_damaged == 2

    # 10 dispatched = 7 good + 2 damaged + 1 missing.
    log = reconciler.reconcile_transfer(
        transfer.id,
        reconciled_by="Inventory Auditor",
    )

    assert log.total_dispatched_units == 10
    assert log.total_received_units == 7
    assert log.variance_units == 3
    assert log.total_transit_loss_value == 300.0
    assert "Damaged units: 2" in log.remarks
    assert "Missing units: 1" in log.remarks
    assert transfer.status == TransferStatus.DISPUTED


def test_dispatch_deducts_source_and_receipt_adds_good_stock(
    db_session,
):
    create_branches(db_session)
    service = StockTransferService(db_session)

    transfer = service.create_transfer_request(
        source_branch_id="b1",
        destination_branch_id="b2",
        items=[
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "quantity_dispatched": 5,
                "unit_cost": 100.0,
            }
        ],
        dispatched_by="tester",
    )

    service.dispatch_transfer(transfer.id)

    source_batch = (
        db_session.query(StockBatch)
        .filter_by(
            branch_id="b1",
            product_id="p1",
            batch_number="BATCH-001",
        )
        .one()
    )

    assert source_batch.quantity == 995

    service.start_transit(transfer.id)

    transfer = db_session.query(StockTransfer).filter_by(
        id=transfer.id
    ).one()

    item = transfer.items[0]

    service.receive_and_record_delivery(
        transfer_id=transfer.id,
        received_items=[
            {
                "item_id": item.id,
                "quantity_received": 4,
                "quantity_damaged": 1,
            }
        ],
        received_by="receiver",
    )

    destination_batch = (
        db_session.query(StockBatch)
        .filter_by(
            branch_id="b2",
            product_id="p1",
            batch_number="BATCH-001",
        )
        .one()
    )

    assert destination_batch.quantity == 4
    assert source_batch.quantity == 995


def test_dispatch_rejects_insufficient_stock(db_session):
    create_branches(db_session)
    service = StockTransferService(db_session)

    source_batch = (
        db_session.query(StockBatch)
        .filter_by(
            branch_id="b1",
            product_id="p1",
            batch_number="BATCH-001",
        )
        .one()
    )

    source_batch.quantity = 2
    db_session.commit()

    transfer = service.create_transfer_request(
        source_branch_id="b1",
        destination_branch_id="b2",
        items=[
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "quantity_dispatched": 5,
                "unit_cost": 100.0,
            }
        ],
        dispatched_by="tester",
    )

    with pytest.raises(ValidationError):
        service.dispatch_transfer(transfer.id)

    db_session.rollback()
    db_session.refresh(source_batch)

    transfer = db_session.query(StockTransfer).filter_by(
        id=transfer.id
    ).one()

    assert source_batch.quantity == 2
    assert transfer.status == TransferStatus.DRAFT


def test_dispatch_allocates_across_rows_with_same_batch_number(db_session):
    """Dispatch can allocate one batch across multiple stock rows."""
    create_branches(db_session)

    db_session.add(
        StockBatch(
            id="batch-b1-p1-extra",
            product_id="p1",
            branch_id="b1",
            batch_number="BATCH-001",
            quantity=3,
            purchase_price=100.0,
            selling_price=120.0,
        )
    )
    db_session.commit()

    service = StockTransferService(db_session)
    transfer = service.create_transfer_request(
        source_branch_id="b1",
        destination_branch_id="b2",
        items=[
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "batch_number": "BATCH-001",
                "quantity_dispatched": 1002,
                "unit_cost": 100.0,
            }
        ],
        dispatched_by="tester",
    )

    dispatched = service.dispatch_transfer(transfer.id)

    remaining = (
        db_session.query(StockBatch)
        .filter_by(
            branch_id="b1",
            product_id="p1",
            batch_number="BATCH-001",
        )
        .with_entities(StockBatch.quantity)
        .all()
    )

    assert sum(quantity for (quantity,) in remaining) == 1
    assert dispatched.items[0].batch_number == "BATCH-001"
    assert dispatched.status == TransferStatus.DISPATCHED


def test_dispatch_rolls_back_when_later_item_lacks_stock(db_session):
    """A failed multi-item dispatch must not partially deduct stock."""
    create_branches(db_session)

    before = {
        batch.id: batch.quantity
        for batch in db_session.query(StockBatch).all()
    }

    service = StockTransferService(db_session)
    transfer = service.create_transfer_request(
        source_branch_id="b1",
        destination_branch_id="b2",
        items=[
            {
                "product_id": "p1",
                "product_name": "Available Product",
                "batch_number": "BATCH-001",
                "quantity_dispatched": 10,
                "unit_cost": 100.0,
            },
            {
                "product_id": "p2",
                "product_name": "Unavailable Product",
                "batch_number": "BATCH-002",
                "quantity_dispatched": 1,
                "unit_cost": 40.0,
            },
        ],
        dispatched_by="tester",
    )

    with pytest.raises(ValidationError):
        service.dispatch_transfer(transfer.id)

    db_session.rollback()
    db_session.expire_all()

    after = {
        batch.id: batch.quantity
        for batch in db_session.query(StockBatch).all()
    }
    refreshed_transfer = (
        db_session.query(StockTransfer)
        .filter_by(id=transfer.id)
        .one()
    )

    assert after == before
    assert refreshed_transfer.status == TransferStatus.DRAFT
    assert refreshed_transfer.delivery_challan_number is None
    assert refreshed_transfer.gate_pass_number is None


def test_dispatch_rejects_unspecified_product_with_multiple_batches(db_session):
    """Do not guess which batch to dispatch when multiple batches exist."""
    create_branches(db_session)

    db_session.add(
        StockBatch(
            id="batch-b1-p1-second",
            product_id="p1",
            branch_id="b1",
            batch_number="BATCH-002",
            quantity=20,
            purchase_price=100.0,
            selling_price=120.0,
        )
    )
    db_session.commit()

    service = StockTransferService(db_session)
    transfer = service.create_transfer_request(
        source_branch_id="b1",
        destination_branch_id="b2",
        items=[
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "quantity_dispatched": 10,
                "unit_cost": 100.0,
            }
        ],
        dispatched_by="tester",
    )

    with pytest.raises(ValidationError, match="multiple batches"):
        service.dispatch_transfer(transfer.id)

    db_session.rollback()
    db_session.expire_all()

    source_stock = (
        db_session.query(StockBatch)
        .filter_by(branch_id="b1", product_id="p1")
        .with_entities(StockBatch.quantity)
        .all()
    )
    refreshed_transfer = (
        db_session.query(StockTransfer)
        .filter_by(id=transfer.id)
        .one()
    )

    assert sum(quantity for (quantity,) in source_stock) == 1020
    assert refreshed_transfer.status == TransferStatus.DRAFT


def test_reconciliation_rejects_invalid_resolution_status(db_session):
    """Reject unsupported resolution status values."""
    create_branches(db_session)

    service = StockTransferService(db_session)
    reconciler = MultiBranchReconciliationEngine(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "quantity_dispatched": 5,
                "unit_cost": 100.0,
            }
        ],
    )

    service.dispatch_transfer(transfer.id)
    service.start_transit(transfer.id)

    service.receive_and_record_delivery(
        transfer.id,
        [
            {
                "item_id": transfer.items[0].id,
                "quantity_received": 5,
            }
        ],
        received_by="Store Manager",
    )

    with pytest.raises(ValidationError, match="Invalid resolution status"):
        reconciler.reconcile_transfer(
            transfer.id,
            reconciled_by="Inventory Auditor",
            resolution_status="UNKNOWN",
        )

    db_session.rollback()
    db_session.refresh(transfer)

    assert transfer.status == TransferStatus.RECEIVED


# ----------------------------------------------------------------------
# Reconciliation: calculations, duplicates, statuses and approval audit
# ----------------------------------------------------------------------
def make_received_transfer(db_session, lines):
    """Create a RECEIVED transfer.

    lines: list of (product_id, dispatched, unit_cost, good, damaged).
    """
    create_branches(db_session)

    if any(line[0] == "p2" for line in lines):
        db_session.add(
            StockBatch(
                id="batch-b1-p2",
                product_id="p2",
                branch_id="b1",
                batch_number="BATCH-002",
                quantity=500,
                purchase_price=40.0,
                selling_price=60.0,
            )
        )
        db_session.commit()

    service = StockTransferService(db_session)
    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [
            {
                "product_id": product_id,
                "product_name": f"Product {product_id}",
                "quantity_dispatched": dispatched,
                "unit_cost": unit_cost,
            }
            for product_id, dispatched, unit_cost, _, _ in lines
        ],
    )
    service.dispatch_transfer(transfer.id)
    service.start_transit(transfer.id)

    items_by_product = {i.product_id: i for i in transfer.items}
    service.receive_and_record_delivery(
        transfer.id,
        [
            {
                "item_id": items_by_product[product_id].id,
                "quantity_received": good,
                "quantity_damaged": damaged,
            }
            for product_id, _, _, good, damaged in lines
        ],
        received_by="Store Manager",
    )
    db_session.refresh(transfer)
    return transfer


def batch_quantities(db_session):
    return {
        (b.branch_id, b.product_id): b.quantity
        for b in db_session.query(StockBatch).all()
    }


def test_reconciliation_multi_item_totals_and_loss_value(db_session):
    transfer = make_received_transfer(
        db_session,
        [
            ("p1", 10, 100.0, 7, 2),  # 1 missing
            ("p2", 5, 40.0, 3, 0),  # 2 missing
        ],
    )
    reconciler = MultiBranchReconciliationEngine(db_session)

    log = reconciler.reconcile_transfer(
        transfer.id, reconciled_by="Auditor", remarks="Checked"
    )

    # variance = dispatched - good received = 15 - 10
    assert log.total_dispatched_units == 15
    assert log.total_received_units == 10
    assert log.variance_units == 5
    # (2 + 1) * 100 + (0 + 2) * 40
    assert log.total_transit_loss_value == 380.0

    summary = reconciler.build_summary(transfer.id)
    assert summary["dispatched"] == 15
    assert summary["good_received"] == 10
    assert summary["damaged"] == 2
    assert summary["missing"] == 3
    assert summary["variance"] == 5
    assert summary["loss_value"] == 380.0
    assert summary["resolution_status"] == "PENDING"
    assert summary["final_status"] == "DISPUTED"
    assert summary["approval_status"] == "PENDING"
    assert "Checked" in summary["remarks"]


def test_reconciliation_does_not_change_stock(db_session):
    transfer = make_received_transfer(
        db_session,
        [("p1", 10, 100.0, 6, 3)],  # 3 damaged, 1 missing
    )
    before = batch_quantities(db_session)

    # Source deducted once at dispatch; only good units reached b2.
    assert before[("b1", "p1")] == 990
    assert before[("b2", "p1")] == 6

    MultiBranchReconciliationEngine(db_session).reconcile_transfer(
        transfer.id, reconciled_by="Auditor"
    )

    assert batch_quantities(db_session) == before


def test_reconciliation_zero_variance_needs_no_approval(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 8, 50.0, 8, 0)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)

    log = reconciler.reconcile_transfer(
        transfer.id, reconciled_by="Auditor"
    )

    assert log.variance_units == 0
    assert log.total_transit_loss_value == 0.0
    assert log.resolution_status == "RESOLVED"
    assert transfer.status == TransferStatus.RECONCILED

    # RESOLVED here is a derived flag, not an approval record.
    assert reconciler.get_approval_history(transfer.id) == []
    summary = reconciler.build_summary(transfer.id)
    assert summary["approval_status"] == "NOT_REQUIRED"


def test_duplicate_reconciliation_is_rejected(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 10, 100.0, 7, 2)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)

    first = reconciler.reconcile_transfer(
        transfer.id, reconciled_by="First Auditor"
    )
    first_values = (
        first.id,
        first.reconciled_by,
        first.total_transit_loss_value,
    )

    with pytest.raises(ValidationError, match="already been reconciled"):
        reconciler.reconcile_transfer(
            transfer.id, reconciled_by="Second Auditor"
        )

    logs = db_session.query(ReconciliationLog).all()
    assert len(logs) == 1
    assert (
        logs[0].id,
        logs[0].reconciled_by,
        logs[0].total_transit_loss_value,
    ) == first_values


def test_duplicate_reconciliation_rejected_after_zero_variance(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 4, 10.0, 4, 0)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)
    reconciler.reconcile_transfer(transfer.id, reconciled_by="Auditor")

    with pytest.raises(ValidationError, match="already been reconciled"):
        reconciler.reconcile_transfer(transfer.id, reconciled_by="Auditor")

    assert db_session.query(ReconciliationLog).count() == 1
    assert transfer.status == TransferStatus.RECONCILED


@pytest.mark.parametrize(
    "stop_after",
    ["draft", "dispatched", "in_transit"],
)
def test_reconcile_rejects_invalid_statuses(db_session, stop_after):
    create_branches(db_session)
    service = StockTransferService(db_session)
    reconciler = MultiBranchReconciliationEngine(db_session)

    transfer = service.create_transfer_request(
        "b1",
        "b2",
        [
            {
                "product_id": "p1",
                "product_name": "Test Product",
                "quantity_dispatched": 5,
                "unit_cost": 10.0,
            }
        ],
    )
    if stop_after in ("dispatched", "in_transit"):
        service.dispatch_transfer(transfer.id)
    if stop_after == "in_transit":
        service.start_transit(transfer.id)

    status_before = transfer.status

    with pytest.raises(ValidationError, match="before delivery"):
        reconciler.reconcile_transfer(
            transfer.id, reconciled_by="Auditor"
        )

    db_session.refresh(transfer)
    assert transfer.status == status_before
    assert db_session.query(ReconciliationLog).count() == 0


def test_reconcile_rejects_unknown_transfer_and_blank_name(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 5, 10.0, 5, 0)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)

    with pytest.raises(Exception, match="not found"):
        reconciler.reconcile_transfer("missing-id", reconciled_by="A")

    with pytest.raises(ValidationError, match="Reconciled-by"):
        reconciler.reconcile_transfer(transfer.id, reconciled_by="   ")

    assert db_session.query(ReconciliationLog).count() == 0


def test_resolved_not_allowed_with_variance_at_reconcile(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 10, 100.0, 7, 2)]
    )

    with pytest.raises(ValidationError, match="approval"):
        MultiBranchReconciliationEngine(db_session).reconcile_transfer(
            transfer.id,
            reconciled_by="Auditor",
            resolution_status="RESOLVED",
        )

    assert db_session.query(ReconciliationLog).count() == 0


def test_eligibility_helpers(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 10, 100.0, 7, 2)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)

    assert reconciler.can_reconcile(transfer) is True
    assert reconciler.can_review_dispute(transfer) is False

    reconciler.reconcile_transfer(transfer.id, reconciled_by="Auditor")
    db_session.refresh(transfer)

    assert reconciler.can_reconcile(transfer) is False
    assert reconciler.can_review_dispute(transfer) is True

    reconciler.record_approval(transfer.id, "Owner", "APPROVED")
    db_session.refresh(transfer)

    assert reconciler.can_reconcile(transfer) is False
    assert reconciler.can_review_dispute(transfer) is False


def test_approval_records_audit_history(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 10, 100.0, 7, 2)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)
    log = reconciler.reconcile_transfer(
        transfer.id, reconciled_by="Auditor"
    )
    stock_before = batch_quantities(db_session)

    approval = reconciler.record_approval(
        transfer.id,
        approver="  Owner  ",
        decision="approved",
        remarks="Loss accepted",
    )

    db_session.refresh(transfer)
    db_session.refresh(log)

    assert approval.approver == "Owner"
    assert approval.decision == "APPROVED"
    assert approval.remarks == "Loss accepted"
    assert approval.decided_at is not None
    assert approval.transfer_id == transfer.id
    assert approval.reconciliation_id == log.id

    assert log.resolution_status == "RESOLVED"
    assert transfer.status == TransferStatus.RECONCILED
    assert batch_quantities(db_session) == stock_before

    history = reconciler.get_approval_history(transfer.id)
    assert [h.id for h in history] == [approval.id]
    assert reconciler.build_summary(transfer.id)["approval_status"] == (
        "APPROVED"
    )


def test_rejection_keeps_dispute_and_history_accumulates(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 10, 100.0, 7, 2)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)
    log = reconciler.reconcile_transfer(
        transfer.id, reconciled_by="Auditor"
    )

    with pytest.raises(ValidationError, match="Remarks are required"):
        reconciler.record_approval(transfer.id, "Owner", "REJECTED")
    assert db_session.query(ReconciliationApproval).count() == 0

    reconciler.record_approval(
        transfer.id, "Owner", "REJECTED", remarks="Recount needed"
    )
    db_session.refresh(transfer)
    db_session.refresh(log)

    assert transfer.status == TransferStatus.DISPUTED
    assert log.resolution_status == "PENDING"
    assert reconciler.can_review_dispute(transfer) is True

    reconciler.record_approval(transfer.id, "Director", "APPROVED")
    db_session.refresh(transfer)

    history = reconciler.get_approval_history(transfer.id)
    assert [(h.approver, h.decision) for h in history] == [
        ("Owner", "REJECTED"),
        ("Director", "APPROVED"),
    ]
    assert transfer.status == TransferStatus.RECONCILED


def test_approval_rejected_for_invalid_state_or_input(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 10, 100.0, 7, 2)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)

    # Not reconciled yet: no log to approve.
    with pytest.raises(ValidationError, match="not been reconciled"):
        reconciler.record_approval(transfer.id, "Owner", "APPROVED")

    reconciler.reconcile_transfer(transfer.id, reconciled_by="Auditor")

    with pytest.raises(ValidationError, match="Approver name"):
        reconciler.record_approval(transfer.id, "  ", "APPROVED")

    with pytest.raises(ValidationError, match="Invalid decision"):
        reconciler.record_approval(transfer.id, "Owner", "MAYBE")

    reconciler.record_approval(transfer.id, "Owner", "APPROVED")

    # Already resolved: a second decision must not be accepted.
    with pytest.raises(ValidationError, match="Only DISPUTED"):
        reconciler.record_approval(transfer.id, "Owner", "APPROVED")

    assert db_session.query(ReconciliationApproval).count() == 1


def test_approval_rejected_for_zero_variance_transfer(db_session):
    transfer = make_received_transfer(
        db_session, [("p1", 3, 10.0, 3, 0)]
    )
    reconciler = MultiBranchReconciliationEngine(db_session)
    reconciler.reconcile_transfer(transfer.id, reconciled_by="Auditor")

    with pytest.raises(ValidationError, match="Only DISPUTED"):
        reconciler.record_approval(transfer.id, "Owner", "APPROVED")

    assert db_session.query(ReconciliationApproval).count() == 0