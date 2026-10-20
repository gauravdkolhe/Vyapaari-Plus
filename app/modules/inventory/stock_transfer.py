
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import EntityNotFoundError, ValidationError
from app.modules.inventory.models import (
    CarrierLog,
    StockBatch,
    StockTransfer,
    TransferItem,
    TransferStatus,
)


class StockTransferService:
    """Manage stock transfer lifecycle, delivery and carrier tracking."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _get_transfer(self, transfer_id: str) -> StockTransfer:
        """Fetch a transfer with a row lock where supported by the database."""
        transfer = (
            self.db.query(StockTransfer)
            .filter(StockTransfer.id == transfer_id)
            .with_for_update()
            .first()
        )

        if transfer is None:
            raise EntityNotFoundError("StockTransfer", transfer_id)

        return transfer

    @staticmethod
    def _document_number(prefix: str) -> str:
        date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
        unique_part = uuid.uuid4().hex[:10].upper()
        return f"{prefix}-{date_part}-{unique_part}"

    def _add_carrier_log(
        self,
        transfer: StockTransfer,
        event_type: str,
        location: str = None,
        details: str = None,
        logged_by: str = None,
    ) -> CarrierLog:
        """Add an event to the current transaction."""
        log = CarrierLog(
            id=str(uuid.uuid4()),
            transfer_id=transfer.id,
            event_type=event_type,
            location=location,
            details=details,
            logged_by=logged_by,
            logged_at=self._utc_now(),
        )
        self.db.add(log)
        return log

    def create_transfer_request(
        self,
        source_branch_id: str,
        destination_branch_id: str,
        items: list[dict],
        dispatched_by: str = None,
        notes: str = None,
    ) -> StockTransfer:
        """Create a validated draft transfer and its audit event."""

        if not source_branch_id or not destination_branch_id:
            raise ValidationError(
                "Source and destination branch IDs are required."
            )

        if source_branch_id == destination_branch_id:
            raise ValidationError(
                "Source and destination branches cannot be the same."
            )

        if not isinstance(items, list) or not items:
            raise ValidationError(
                "Transfer must contain at least one item."
            )

        validated_items = []

        for item_data in items:
            if not isinstance(item_data, dict):
                raise ValidationError(
                    "Each transfer item must be a dictionary."
                )

            product_id = item_data.get("product_id")
            if not isinstance(product_id, str) or not product_id.strip():
                raise ValidationError("Product ID is required.")

            quantity = item_data.get("quantity_dispatched")
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
            ):
                raise ValidationError(
                    "Dispatched quantity must be a positive integer."
                )

            unit_cost = item_data.get("unit_cost", 0.0)
            if (
                isinstance(unit_cost, bool)
                or not isinstance(unit_cost, (int, float))
                or unit_cost < 0
            ):
                raise ValidationError(
                    "Unit cost must be a non-negative number."
                )

            batch_number = item_data.get("batch_number")
            if batch_number is not None and (
                not isinstance(batch_number, str)
                or not batch_number.strip()
            ):
                raise ValidationError(
                    "Batch number must be a non-empty string when provided."
                )

            validated_items.append(
                {
                    "product_id": product_id.strip(),
                    "product_name": item_data.get(
                        "product_name", "Unknown Product"
                    ),
                    "batch_number": (
                        batch_number.strip()
                        if batch_number is not None
                        else None
                    ),
                    "quantity_dispatched": quantity,
                    "unit_cost": float(unit_cost),
                }
            )

        transfer = StockTransfer(
            id=str(uuid.uuid4()),
            transfer_number=self._document_number("TRF"),
            source_branch_id=source_branch_id,
            destination_branch_id=destination_branch_id,
            status=TransferStatus.DRAFT,
            dispatched_by=dispatched_by,
            notes=notes,
        )

        try:
            self.db.add(transfer)
            self.db.flush()

            for item_data in validated_items:
                self.db.add(
                    TransferItem(
                        id=str(uuid.uuid4()),
                        transfer_id=transfer.id,
                        product_id=item_data["product_id"],
                        product_name=item_data["product_name"],
                        batch_number=item_data["batch_number"],
                        quantity_dispatched=item_data[
                            "quantity_dispatched"
                        ],
                        quantity_received=0,
                        quantity_damaged=0,
                        unit_cost=item_data["unit_cost"],
                    )
                )

            self._add_carrier_log(
                transfer,
                event_type="TRANSFER_CREATED",
                details="Stock transfer request created.",
                logged_by=dispatched_by,
            )

            self.db.commit()
            self.db.refresh(transfer)
            return transfer

        except Exception:
            self.db.rollback()
            raise

    def generate_delivery_documents(
        self,
        transfer_id: str,
    ) -> StockTransfer:
        """Generate delivery challan and gate pass for a draft transfer."""

        transfer = self._get_transfer(transfer_id)

        if transfer.status != TransferStatus.DRAFT:
            raise ValidationError(
                "Delivery documents can only be generated "
                "for a DRAFT transfer."
            )

        if not transfer.items:
            raise ValidationError(
                "Cannot generate documents for a transfer without items."
            )

        try:
            if not transfer.delivery_challan_number:
                transfer.delivery_challan_number = (
                    self._document_number("CHL")
                )

            if not transfer.gate_pass_number:
                transfer.gate_pass_number = (
                    self._document_number("GATE")
                )

            self._add_carrier_log(
                transfer,
                event_type="DOCUMENTS_GENERATED",
                details=(
                    f"Challan: {transfer.delivery_challan_number}; "
                    f"Gate pass: {transfer.gate_pass_number}"
                ),
            )

            self.db.commit()
            self.db.refresh(transfer)
            return transfer

        except Exception:
            self.db.rollback()
            raise

    def _deduct_source_stock(self, transfer: StockTransfer) -> None:
        """Validate and allocate stock from locked source batches."""

        batches = (
            self.db.query(StockBatch)
            .filter(StockBatch.branch_id == transfer.source_branch_id)
            .order_by(
                StockBatch.product_id,
                StockBatch.batch_number,
                StockBatch.id,
            )
            .with_for_update()
            .all()
        )

        requirements = {}
        item_keys = {}
        batch_modes_by_product = {}

        for item in transfer.items:
            requested_batch = item.batch_number or None
            key = (item.product_id, requested_batch)

            item_keys[item.id] = key
            requirements[key] = requirements.get(key, 0) + (
                item.quantity_dispatched
            )

            modes = batch_modes_by_product.setdefault(
                item.product_id, set()
            )
            modes.add(requested_batch is None)

        # Avoid overlapping reservations for specified and unspecified batches.
        for product_id, modes in batch_modes_by_product.items():
            if len(modes) > 1:
                raise ValidationError(
                    f"Product {product_id} has both specified and "
                    "unspecified batch items. Specify a batch number "
                    "for every item of this product."
                )

        candidates_by_key = {}

        # Validate every requirement before mutating any stock.
        for (product_id, requested_batch), required in requirements.items():
            candidates = [
                batch
                for batch in batches
                if batch.product_id == product_id
                and (
                    requested_batch is None
                    or batch.batch_number == requested_batch
                )
            ]

            available = sum(batch.quantity for batch in candidates)

            if available < required:
                raise ValidationError(
                    f"Insufficient stock for product {product_id}. "
                    f"Required: {required}; available: {available}."
                )

            batch_numbers = {batch.batch_number for batch in candidates}

            # One TransferItem stores only one batch number.
            if requested_batch is None and len(batch_numbers) > 1:
                raise ValidationError(
                    f"Product {product_id} has stock in multiple batches. "
                    "Specify a batch number for the transfer item."
                )

            if not candidates:
                raise ValidationError(
                    f"Insufficient stock for product {product_id}. "
                    f"Required: {required}; available: 0."
                )

            candidates_by_key[(product_id, requested_batch)] = candidates

        # All stock requirements have been validated. Deduct quantities.
        for item in transfer.items:
            key = item_keys[item.id]
            candidates = candidates_by_key[key]
            remaining = item.quantity_dispatched

            for batch in candidates:
                if remaining <= 0:
                    break

                deducted = min(batch.quantity, remaining)
                batch.quantity -= deducted
                remaining -= deducted

            if remaining > 0:
                raise ValidationError(
                    f"Insufficient stock for product {item.product_id}; "
                    f"{remaining} units could not be allocated."
                )

            item.batch_number = candidates[0].batch_number

    def dispatch_transfer(
        self,
        transfer_id: str,
        vehicle_number: str = None,
        driver_contact: str = None,
    ) -> StockTransfer:
        """Dispatch a draft transfer and create delivery documents."""

        transfer = self._get_transfer(transfer_id)

        if transfer.status != TransferStatus.DRAFT:
            raise ValidationError(
                f"Cannot dispatch transfer with status: "
                f"{transfer.status.value}"
            )

        if not transfer.items:
            raise ValidationError(
                "Cannot dispatch a transfer without items."
            )

        try:
            self._deduct_source_stock(transfer)

            if not transfer.delivery_challan_number:
                transfer.delivery_challan_number = (
                    self._document_number("CHL")
                )

            if not transfer.gate_pass_number:
                transfer.gate_pass_number = (
                    self._document_number("GATE")
                )

            transfer.status = TransferStatus.DISPATCHED
            transfer.dispatched_at = self._utc_now()
            transfer.vehicle_number = vehicle_number
            transfer.driver_contact = driver_contact

            self._add_carrier_log(
                transfer,
                event_type="DISPATCHED",
                details=(
                    f"Vehicle: {vehicle_number or 'Not provided'}; "
                    f"Challan: {transfer.delivery_challan_number}; "
                    f"Gate pass: {transfer.gate_pass_number}"
                ),
                logged_by=transfer.dispatched_by,
            )

            self.db.commit()
            self.db.refresh(transfer)
            return transfer

        except Exception:
            self.db.rollback()
            raise

    def start_transit(self, transfer_id: str) -> StockTransfer:
        """Move a dispatched transfer into transit."""

        transfer = self._get_transfer(transfer_id)

        if transfer.status != TransferStatus.DISPATCHED:
            raise ValidationError(
                "Only DISPATCHED transfers can enter transit. "
                f"Current status: {transfer.status.value}"
            )

        try:
            transfer.status = TransferStatus.IN_TRANSIT

            self._add_carrier_log(
                transfer,
                event_type="IN_TRANSIT",
                details="Transfer has started transit.",
            )

            self.db.commit()
            self.db.refresh(transfer)
            return transfer

        except Exception:
            self.db.rollback()
            raise

    def update_carrier_tracking(
        self,
        transfer_id: str,
        carrier_name: str = None,
        estimated_delivery_at: datetime = None,
        vehicle_number: str = None,
        driver_contact: str = None,
        updated_by: str = None,
    ) -> StockTransfer:
        """Update carrier, ETA, vehicle and driver information."""

        transfer = self._get_transfer(transfer_id)

        if transfer.status not in (
            TransferStatus.DISPATCHED,
            TransferStatus.IN_TRANSIT,
        ):
            raise ValidationError(
                "Carrier tracking can only be updated for "
                "DISPATCHED or IN_TRANSIT transfers."
            )

        if carrier_name is not None:
            if not isinstance(carrier_name, str) or not carrier_name.strip():
                raise ValidationError("Carrier name cannot be empty.")

        if estimated_delivery_at is not None:
            if not isinstance(estimated_delivery_at, datetime):
                raise ValidationError(
                    "Estimated delivery time must be a datetime."
                )

            if (
                estimated_delivery_at.tzinfo is None
                or estimated_delivery_at.utcoffset() is None
            ):
                raise ValidationError(
                    "Estimated delivery time must include a timezone."
                )

        try:
            if carrier_name is not None:
                transfer.carrier_name = carrier_name.strip()

            if estimated_delivery_at is not None:
                transfer.estimated_delivery_at = estimated_delivery_at

            if vehicle_number is not None:
                transfer.vehicle_number = vehicle_number

            if driver_contact is not None:
                transfer.driver_contact = driver_contact

            self._add_carrier_log(
                transfer,
                event_type="TRACKING_UPDATED",
                details=(
                    f"Carrier: {transfer.carrier_name or 'Not provided'}; "
                    f"Vehicle: {transfer.vehicle_number or 'Not provided'}"
                ),
                logged_by=updated_by,
            )

            self.db.commit()
            self.db.refresh(transfer)
            return transfer

        except Exception:
            self.db.rollback()
            raise

    def record_carrier_event(
        self,
        transfer_id: str,
        event_type: str,
        location: str = None,
        details: str = None,
        logged_by: str = None,
    ) -> CarrierLog:
        """Record a carrier checkpoint or movement event."""

        transfer = self._get_transfer(transfer_id)

        if transfer.status not in (
            TransferStatus.DISPATCHED,
            TransferStatus.IN_TRANSIT,
        ):
            raise ValidationError(
                "Carrier events can only be recorded for "
                "DISPATCHED or IN_TRANSIT transfers."
            )

        if not isinstance(event_type, str) or not event_type.strip():
            raise ValidationError("Carrier event type is required.")

        try:
            log = self._add_carrier_log(
                transfer,
                event_type=event_type.strip().upper(),
                location=location,
                details=details,
                logged_by=logged_by,
            )

            self.db.commit()
            self.db.refresh(log)
            return log

        except Exception:
            self.db.rollback()
            raise

    def get_tracking_history(
        self,
        transfer_id: str,
    ) -> list[CarrierLog]:
        """Return tracking events in chronological order."""

        self._get_transfer(transfer_id)

        return (
            self.db.query(CarrierLog)
            .filter(CarrierLog.transfer_id == transfer_id)
            .order_by(CarrierLog.logged_at.asc(), CarrierLog.id.asc())
            .all()
        )

    def receive_and_record_delivery(
        self,
        transfer_id: str,
        received_items: list[dict],
        received_by: str = None,
    ) -> StockTransfer:
        """Record good and damaged quantities after validating delivery."""

        transfer = self._get_transfer(transfer_id)

        if transfer.status != TransferStatus.IN_TRANSIT:
            raise ValidationError(
                "Cannot receive a transfer that is not IN_TRANSIT. "
                f"Current status: {transfer.status.value}"
            )

        if not isinstance(received_items, list) or not received_items:
            raise ValidationError(
                "Received items must be a non-empty list."
            )

        items_by_id = {item.id: item for item in transfer.items}
        received_by_id = {}

        for entry in received_items:
            if not isinstance(entry, dict):
                raise ValidationError(
                    "Each received item must be a dictionary."
                )

            item_id = entry.get("item_id")

            if not item_id or item_id not in items_by_id:
                raise ValidationError(
                    f"Invalid transfer item ID: {item_id}"
                )

            if item_id in received_by_id:
                raise ValidationError(
                    f"Duplicate receipt entry for item: {item_id}"
                )

            good_qty = entry.get("quantity_received")
            damaged_qty = entry.get("quantity_damaged", 0)

            for label, quantity in (
                ("Received quantity", good_qty),
                ("Damaged quantity", damaged_qty),
            ):
                if (
                    isinstance(quantity, bool)
                    or not isinstance(quantity, int)
                    or quantity < 0
                ):
                    raise ValidationError(
                        f"{label} must be a non-negative integer."
                    )

            dispatched_qty = items_by_id[item_id].quantity_dispatched

            if good_qty + damaged_qty > dispatched_qty:
                raise ValidationError(
                    "Good and damaged quantities cannot exceed "
                    "the dispatched quantity."
                )

            received_by_id[item_id] = {
                "good": good_qty,
                "damaged": damaged_qty,
            }

        expected_ids = set(items_by_id)
        supplied_ids = set(received_by_id)

        if expected_ids != supplied_ids:
            missing_ids = sorted(expected_ids - supplied_ids)
            raise ValidationError(
                f"Missing receipt entries for items: {missing_ids}"
            )

        try:
            for item_id, quantities in received_by_id.items():
                item = items_by_id[item_id]
                good_qty = quantities["good"]

                item.quantity_received = good_qty
                item.quantity_damaged = quantities["damaged"]

                # Damaged and missing units must not increase usable stock.
                if good_qty == 0:
                    continue

                if not item.batch_number:
                    raise ValidationError(
                        f"Batch number missing for transfer item {item.id}."
                    )

                destination_batch = (
                    self.db.query(StockBatch)
                    .filter(
                        StockBatch.branch_id
                        == transfer.destination_branch_id,
                        StockBatch.product_id == item.product_id,
                        StockBatch.batch_number == item.batch_number,
                    )
                    .with_for_update()
                    .first()
                )

                if destination_batch is None:
                    source_batch = (
                        self.db.query(StockBatch)
                        .filter(
                            StockBatch.branch_id
                            == transfer.source_branch_id,
                            StockBatch.product_id == item.product_id,
                            StockBatch.batch_number == item.batch_number,
                        )
                        .first()
                    )

                    destination_batch = StockBatch(
                        id=str(uuid.uuid4()),
                        product_id=item.product_id,
                        branch_id=transfer.destination_branch_id,
                        batch_number=item.batch_number,
                        quantity=good_qty,
                        purchase_price=(
                            source_batch.purchase_price
                            if source_batch is not None
                            else item.unit_cost
                        ),
                        selling_price=(
                            source_batch.selling_price
                            if source_batch is not None
                            else 0
                        ),
                        expiry_date=(
                            source_batch.expiry_date
                            if source_batch is not None
                            else None
                        ),
                    )
                    self.db.add(destination_batch)
                else:
                    destination_batch.quantity += good_qty

            now = self._utc_now()
            transfer.status = TransferStatus.RECEIVED
            transfer.received_at = now
            transfer.delivered_at = now
            transfer.received_by = received_by

            total_dispatched = sum(
                item.quantity_dispatched for item in transfer.items
            )
            total_good = sum(
                item.quantity_received for item in transfer.items
            )
            total_damaged = sum(
                item.quantity_damaged for item in transfer.items
            )
            total_missing = sum(
                item.quantity_dispatched
                - item.quantity_received
                - item.quantity_damaged
                for item in transfer.items
            )

            self._add_carrier_log(
                transfer,
                event_type="DELIVERED",
                details=(
                    f"Dispatched: {total_dispatched}; "
                    f"Good received: {total_good}; "
                    f"Damaged: {total_damaged}; "
                    f"Missing: {total_missing}"
                ),
                logged_by=received_by,
            )

            self.db.commit()
            self.db.refresh(transfer)
            return transfer

        except Exception:
            self.db.rollback()
            raise
