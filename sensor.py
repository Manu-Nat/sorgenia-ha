"""Sensors for Sorgenia bill, consumption and curve history."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CURRENCY_EURO, EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SorgeniaCoordinator
from .const import DOMAIN

def _device_info(coordinator, entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=getattr(coordinator.entry, "title", None) or "Sorgenia",
        manufacturer="Sorgenia (unofficial)",
        model="Customer area",
        entry_type=DeviceEntryType.SERVICE,
    )



def _number(value):
    try:
        return float(Decimal(str(value).replace(",", ".")))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _latest_bill(bills: list[dict]) -> dict:
    if not bills:
        return {}
    dated = [bill for bill in bills if bill.get("issueDate")]

    def sort_key(bill):
        value = str(bill.get("issueDate", ""))
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:10], fmt)
            except ValueError:
                continue
        return datetime.min

    return max(dated, key=sort_key) if dated else bills[0]


_ELECTRICITY_CODES = ("ele", "electricity")
_GAS_CODES = ("gas",)

# key -> (commodity codes, bill field)
_COMMODITY_BILL_KEYS = {
    "latest_electricity_issue_date": (_ELECTRICITY_CODES, "issueDate"),
    "latest_electricity_due_date": (_ELECTRICITY_CODES, "dueDate"),
    "latest_electricity_status": (_ELECTRICITY_CODES, "statusLabel"),
    "latest_gas_issue_date": (_GAS_CODES, "issueDate"),
    "latest_gas_due_date": (_GAS_CODES, "dueDate"),
    "latest_gas_status": (_GAS_CODES, "statusLabel"),
}
_ELE_CONSUMPTION_KEYS = ("latest_ele_consumption", "ele_consumption_total", "ele_consumption_count")
_GAS_CONSUMPTION_KEYS = ("latest_consumption", "consumption_total", "consumption_count")


def _bill_details(bill: dict) -> dict:
    details = {
        "invoice_id": bill.get("invoiceId"),
        "commodity": bill.get("commodity"),
        "amount": bill.get("amount"),
        "competence_start": bill.get("competenceStartDate"),
        "competence_end": bill.get("competenceEndDate"),
        "issue_date": bill.get("issueDate"),
        "due_date": bill.get("dueDate"),
        "status": bill.get("statusLabel") or bill.get("invoiceStatus"),
        "status_description": bill.get("statusDescription"),
        "partial_amount_paid": bill.get("partialAmountPaid"),
        "payment_method": bill.get("paymentMethod"),
        "download_action": bill.get("downloadCtaCode"),
        "product_name": bill.get("productName"),
        "consumption_type": bill.get("consumptionType"),
    }
    return {key: value for key, value in details.items() if value is not None}


def _consumption_attributes(items: list[dict]) -> dict:
    latest = _latest(items)
    return {
        "latest_competence_start": latest.get("dataCompetenzaStart"),
        "latest_competence_end": latest.get("dataCompetenzaEnd"),
        "latest_issue_date": latest.get("dataEmissione"),
        "consumption_history": [
            {
                key: item.get(key)
                for key in ("dataEmissione", "dataCompetenzaStart", "dataCompetenzaEnd", "consumo")
                if item.get(key) is not None
            }
            for item in items
        ],
    }


# key -> (commodity, kind)
_BILL_COST_KEYS = {
    "latest_electricity_bill_costs": ("electricity", "amount"),
    "latest_gas_bill_costs": ("gas", "amount"),
    "latest_electricity_unit_cost": ("electricity", "unit"),
    "latest_gas_unit_cost": ("gas", "unit"),
}
_GAS_CURVE_KEYS = ("latest_gas_curve_consumption", "gas_curve_total", "gas_curve_months")
_GAS_METER_KEYS = ("gas_meter_reading", "gas_meter_reading_date")


def _parse_dt(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


def _monthly_rows(items: list[dict], billed_items: list[dict] | None = None) -> list[dict]:
    """Keep one full-month curve row per period, preferring a matching billed value."""
    billed_by_month = {}
    for item in billed_items or []:
        start = _parse_dt(item.get("dataCompetenzaStart"))
        amount = _number(item.get("consumo"))
        if start and amount is not None:
            billed_by_month[(start.year, start.month)] = amount

    rows_by_period = {}
    for item in items:
        start = _parse_dt(item.get("dataCompetenzaStart"))
        end = _parse_dt(item.get("dataCompetenzaEnd"))
        if start and end and (end - start).days >= 27:
            period = (start, end)
            current = rows_by_period.get(period)
            if current is None:
                rows_by_period[period] = item
                continue

            # Sorgenia can return two full-month gas rows for the same period.
            # Prefer the row that agrees with the billed consumption, if available;
            # otherwise retain the last row returned by the API.
            billed_amount = billed_by_month.get((start.year, start.month))
            if billed_amount is None:
                rows_by_period[period] = item
                continue
            current_amount = _number(current.get("curveconsumomensile"))
            candidate_amount = _number(item.get("curveconsumomensile"))
            current_matches = current_amount is not None and abs(current_amount - billed_amount) < 0.05
            candidate_matches = candidate_amount is not None and abs(candidate_amount - billed_amount) < 0.05
            if candidate_matches or not current_matches:
                rows_by_period[period] = item
    return list(rows_by_period.values())


def _latest(items: list[dict]) -> dict:
    return max(items, key=lambda item: str(item.get("dataCompetenzaStart", "")), default={})


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: SorgeniaCoordinator = hass.data[DOMAIN][entry.entry_id]
    keys = (
        "latest_amount", "latest_issue_date", "latest_due_date", "latest_status",
        "bill_count", "remaining_total", "latest_gas_amount", "gas_remaining_total",
        "gas_bill_count", "latest_electricity_amount", "electricity_remaining_total",
        "electricity_bill_count", "latest_consumption", "consumption_total",
        "consumption_count", "curve_consumption_total", "latest_curve_consumption",
        "latest_band_1", "latest_band_2", "latest_band_3", "latest_peak_power",
        "curve_month_count", "greeners_status",
        "latest_electricity_issue_date", "latest_electricity_due_date", "latest_electricity_status",
        "latest_gas_issue_date", "latest_gas_due_date", "latest_gas_status",
        "latest_ele_consumption", "ele_consumption_total", "ele_consumption_count",
        "token_expiry",
        "gas_meter_reading", "gas_meter_reading_date",
        "latest_gas_curve_consumption", "gas_curve_total", "gas_curve_months",
        "latest_electricity_bill_costs", "latest_gas_bill_costs",
        "latest_electricity_unit_cost", "latest_gas_unit_cost",
    )
    async_add_entities(SorgeniaBillSensor(coordinator, entry.entry_id, key) for key in keys)


class SorgeniaBillSensor(CoordinatorEntity, SensorEntity):
    """Expose summaries and full histories as entity attributes."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = _device_info(coordinator, entry_id)
        self._attr_name = {
            "latest_amount": "Latest bill amount",
            "latest_issue_date": "Latest bill issue date",
            "latest_due_date": "Latest bill due date",
            "latest_status": "Latest bill status",
            "bill_count": "Bill count",
            "remaining_total": "Total remaining due",
            "latest_gas_amount": "Latest gas bill amount",
            "gas_remaining_total": "Gas remaining due",
            "gas_bill_count": "Gas bill count",
            "latest_electricity_amount": "Latest electricity bill amount",
            "electricity_remaining_total": "Electricity remaining due",
            "electricity_bill_count": "Electricity bill count",
            "latest_consumption": "Latest gas consumption",
            "consumption_total": "Gas consumption total for period",
            "consumption_count": "Gas consumption months",
            "latest_electricity_issue_date": "Latest electricity bill issue date",
            "latest_electricity_due_date": "Latest electricity bill due date",
            "latest_electricity_status": "Latest electricity bill status",
            "latest_gas_issue_date": "Latest gas bill issue date",
            "latest_gas_due_date": "Latest gas bill due date",
            "latest_gas_status": "Latest gas bill status",
            "latest_electricity_bill_costs": "Latest electricity bill cost breakdown",
            "latest_gas_bill_costs": "Latest gas bill cost breakdown",
            "latest_electricity_unit_cost": "Latest electricity all-in unit cost",
            "latest_gas_unit_cost": "Latest gas all-in unit cost",
            "gas_meter_reading": "Gas meter reading",
            "gas_meter_reading_date": "Gas meter reading date",
            "latest_gas_curve_consumption": "Latest gas monthly consumption (curve)",
            "gas_curve_total": "Gas curve consumption total",
            "gas_curve_months": "Gas curve months",
            "token_expiry": "Token expiry",
            "latest_ele_consumption": "Latest electricity billed consumption",
            "ele_consumption_total": "Electricity billed consumption total",
            "ele_consumption_count": "Electricity billed consumption months",
            "curve_consumption_total": "Curve consumption total",
            "latest_curve_consumption": "Latest curve monthly consumption",
            "latest_band_1": "Latest consumption band 1",
            "latest_band_2": "Latest consumption band 2",
            "latest_band_3": "Latest consumption band 3",
            "latest_peak_power": "Latest monthly peak power",
            "curve_month_count": "Curve months",
            "greeners_status": "Greeners status",
        }[key]
        is_energy = key in (
            "curve_consumption_total", "latest_curve_consumption",
            "latest_band_1", "latest_band_2", "latest_band_3",
            "latest_ele_consumption", "ele_consumption_total",
        )
        self._attr_icon = "mdi:lightning-bolt" if is_energy or "curve" in key or "band" in key or "peak" in key else "mdi:receipt-text"
        if key in (
            "latest_amount", "remaining_total", "latest_gas_amount", "gas_remaining_total",
            "latest_electricity_amount", "electricity_remaining_total",
        ):
            self._attr_native_unit_of_measurement = CURRENCY_EURO
            self._attr_suggested_display_precision = 2
        if is_energy:
            self._attr_native_unit_of_measurement = "kWh"
            self._attr_suggested_display_precision = 1
        if key == "latest_peak_power":
            self._attr_native_unit_of_measurement = "kW"
            self._attr_suggested_display_precision = 1
        if key in ("latest_consumption", "consumption_total"):
            # Gas supply consumption (assumed standard cubic metres).
            self._attr_native_unit_of_measurement = "m³"
            self._attr_suggested_display_precision = 1
            self._attr_icon = "mdi:fire"
        if key == "consumption_count":
            self._attr_icon = "mdi:fire"
        if key in ("latest_gas_curve_consumption", "gas_curve_total"):
            self._attr_native_unit_of_measurement = "m³"
            self._attr_suggested_display_precision = 1
        if key in _GAS_CURVE_KEYS:
            self._attr_icon = "mdi:fire"
        if key in ("gas_curve_total", "gas_curve_months"):
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key in _BILL_COST_KEYS:
            kind = _BILL_COST_KEYS[key][1]
            commodity = _BILL_COST_KEYS[key][0]
            if kind == "amount":
                self._attr_native_unit_of_measurement = CURRENCY_EURO
                self._attr_suggested_display_precision = 2
            else:
                self._attr_native_unit_of_measurement = "€/kWh" if commodity == "electricity" else "€/m³"
                self._attr_suggested_display_precision = 3
            self._attr_icon = "mdi:cash-multiple"
        if key == "gas_meter_reading":
            self._attr_native_unit_of_measurement = "m³"
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_suggested_display_precision = 0
            self._attr_icon = "mdi:meter-gas"
        if key == "gas_meter_reading_date":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_icon = "mdi:calendar-clock"
        if key == "token_expiry":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_icon = "mdi:key-clock"
        if key in (
            "bill_count", "remaining_total", "gas_remaining_total", "gas_bill_count",
            "electricity_remaining_total", "electricity_bill_count", "consumption_total", "consumption_count",
            "curve_consumption_total", "curve_month_count",
            "ele_consumption_total", "ele_consumption_count",
        ):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        if self._key == "token_expiry":
            return True  # keep the token diagnostics visible even when data polling fails
        return super().available

    @property
    def native_value(self):
        if self._key == "token_expiry":
            expires_at = self.coordinator.token_expires_at
            if not expires_at:
                return None
            return datetime.fromtimestamp(expires_at, tz=timezone.utc)
        data = self.coordinator.data
        bills = data["bills"]
        consumption = data["consumption"]
        curves = data["curves"]
        latest_bill = _latest_bill(bills)
        latest_consumption = _latest(consumption)
        latest_curve = _latest(curves)

        if self._key == "token_expiry":
            expires_at = self.coordinator.token_expires_at
            if not expires_at:
                return None
            return datetime.fromtimestamp(expires_at, tz=timezone.utc)
        if self._key in _COMMODITY_BILL_KEYS:
            codes, field = _COMMODITY_BILL_KEYS[self._key]
            selected = [b for b in bills if str(b.get("commodity", "")).lower() in codes]
            bill = _latest_bill(selected)
            if field == "statusLabel":
                return bill.get("statusLabel") or bill.get("invoiceStatus")
            return bill.get(field)
        if self._key in _BILL_COST_KEYS:
            commodity, kind = _BILL_COST_KEYS[self._key]
            items = ((data.get("bill_costs") or {}).get(commodity) or {}).get("items") or {}
            total = items.get("importo")
            if kind == "amount":
                return total
            consumption = items.get("consumoTotaleFatturato")
            if total is None or not consumption:
                return None
            return round(total / consumption, 4)
        if self._key in _GAS_CURVE_KEYS:
            rows = _monthly_rows(data.get("gas_curves") or [], data.get("consumption") or [])
            if not rows and data.get("gas_curve_error"):
                return None
            if self._key == "latest_gas_curve_consumption":
                return _number(_latest(rows).get("curveconsumomensile"))
            if self._key == "gas_curve_total":
                return round(sum((_number(r.get("curveconsumomensile")) or 0) for r in rows), 1)
            return len(rows)
        if self._key in _GAS_METER_KEYS:
            meter = data.get("gas_meter") or {}
            if self._key == "gas_meter_reading":
                return _number(meter.get("lettura1"))
            raw = meter.get("dataLettura")
            if not raw:
                return None
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                return None
        if self._key in _ELE_CONSUMPTION_KEYS:
            ele = data.get("consumption_electricity") or []
            if not ele and data.get("consumption_electricity_error"):
                return None
            if self._key == "latest_ele_consumption":
                return _number(_latest(ele).get("consumo"))
            if self._key == "ele_consumption_total":
                return round(sum((_number(item.get("consumo")) or 0) for item in ele), 1)
            return len(ele)

        if self._key == "latest_amount":
            return _number(latest_bill.get("amount"))
        if self._key == "latest_issue_date":
            return latest_bill.get("issueDate")
        if self._key == "latest_due_date":
            return latest_bill.get("dueDate")
        if self._key == "latest_status":
            return latest_bill.get("statusLabel") or latest_bill.get("invoiceStatus")
        if self._key == "bill_count":
            return len(bills)
        if self._key.startswith("gas_") or self._key == "latest_gas_amount":
            selected = [bill for bill in bills if str(bill.get("commodity", "")).lower() == "gas"]
            if self._key == "latest_gas_amount":
                return _number(_latest_bill(selected).get("amount"))
            if self._key == "gas_bill_count":
                return len(selected)
            return round(sum(
                (_number(bill.get("remainingAmountDue")) or 0)
                for bill in selected if bill.get("statusCode") != "PAID"
            ), 2)
        if self._key.startswith("electricity_") or self._key == "latest_electricity_amount":
            selected = [bill for bill in bills if str(bill.get("commodity", "")).lower() in ("ele", "electricity")]
            if self._key == "latest_electricity_amount":
                return _number(_latest_bill(selected).get("amount"))
            if self._key == "electricity_bill_count":
                return len(selected)
            return round(sum(
                (_number(bill.get("remainingAmountDue")) or 0)
                for bill in selected if bill.get("statusCode") != "PAID"
            ), 2)
        if self._key == "remaining_total":
            return round(sum(
                (_number(bill.get("remainingAmountDue")) or 0)
                for bill in bills if bill.get("statusCode") != "PAID"
            ), 2)
        if self._key == "latest_consumption":
            return _number(latest_consumption.get("consumo"))
        if self._key == "consumption_total":
            return round(sum((_number(item.get("consumo")) or 0) for item in consumption), 1)
        if self._key == "consumption_count":
            return len(consumption)
        if self._key == "curve_consumption_total":
            return _number(data.get("curve_total")) or round(
                sum((_number(item.get("curveconsumomensile")) or 0) for item in curves), 1
            )
        if self._key == "latest_curve_consumption":
            return _number(latest_curve.get("curveconsumomensile"))
        if self._key == "latest_band_1":
            return _number(latest_curve.get("fascia1"))
        if self._key == "latest_band_2":
            return _number(latest_curve.get("fascia2"))
        if self._key == "latest_band_3":
            return _number(latest_curve.get("fascia3"))
        if self._key == "latest_peak_power":
            return _number(latest_curve.get("piccopotenzamensile"))
        if self._key == "greeners_status":
            return data["greeners"].get("statusGreeners")
        return len(curves)

    @property
    def extra_state_attributes(self):
        if self._key == "token_expiry":
            coordinator = self.coordinator
            attributes = {
                "last_refresh": coordinator.token_last_refresh.isoformat()
                if coordinator.token_last_refresh
                else None,
                "refresh_method": coordinator.token_refresh_method,
                "refresh_token_stored": coordinator.has_refresh_token,
                "refresh_error": coordinator.token_error,
                "integration_version": coordinator.integration_version,
            }
            return {k: v for k, v in attributes.items() if v is not None}
        data = self.coordinator.data
        bills = data["bills"]
        consumption = data["consumption"]
        curves = data["curves"]
        if self._key == "token_expiry":
            coordinator = self.coordinator
            attributes = {
                "last_refresh": coordinator.token_last_refresh.isoformat()
                if coordinator.token_last_refresh
                else None,
                "refresh_method": coordinator.token_refresh_method,
                "refresh_token_stored": coordinator.has_refresh_token,
                "refresh_error": coordinator.token_error,
            }
            return {k: v for k, v in attributes.items() if v is not None}
        if self._key in _COMMODITY_BILL_KEYS:
            codes, _field = _COMMODITY_BILL_KEYS[self._key]
            selected = [b for b in bills if str(b.get("commodity", "")).lower() in codes]
            return _bill_details(_latest_bill(selected))
        if self._key in _BILL_COST_KEYS:
            commodity = _BILL_COST_KEYS[self._key][0]
            cost = (data.get("bill_costs") or {}).get(commodity) or {}
            attributes = {
                "invoice_id": cost.get("invoice_id"),
                "product": cost.get("product"),
                **(cost.get("items") or {}),
                "endpoint_error": cost.get("error"),
            }
            return {k: v for k, v in attributes.items() if v is not None}
        if self._key in _GAS_CURVE_KEYS:
            raw_rows = data.get("gas_curves") or []
            rows = _monthly_rows(raw_rows, data.get("consumption") or [])
            keys = ("dataCompetenzaStart", "dataCompetenzaEnd", "curveconsumomensile")
            attributes = {
                "latest_curve": {k: v for k, v in _latest(rows).items() if k in keys},
                "curve_consumption_history": [{k: v for k, v in r.items() if k in keys} for r in rows],
                "curve_rows_received": len(raw_rows),
                "unique_full_months": len(rows),
                "api_total_including_partial_rows": data.get("gas_curve_api_total"),
            }
            if data.get("gas_curve_error"):
                attributes["endpoint_error"] = data["gas_curve_error"]
            return {k: v for k, v in attributes.items() if v is not None}
        if self._key in _GAS_METER_KEYS:
            meter = data.get("gas_meter") or {}
            info = data.get("gas_meter_info") or {}
            attributes = {
                "reading_date": meter.get("dataLettura"),
                "source": meter.get("descrizioneFonte"),
                "channel": meter.get("canaleProvenienza"),
                "reading_2": meter.get("lettura2"),
                "reading_3": meter.get("lettura3"),
                "corrector_applied": info.get("flagCORR"),
                "corrector_value": info.get("valoreCORR"),
                "endpoint_error": data.get("gas_meter_error"),
            }
            return {k: v for k, v in attributes.items() if v is not None}
        if self._key in _ELE_CONSUMPTION_KEYS:
            attributes = _consumption_attributes(data.get("consumption_electricity") or [])
            if data.get("consumption_electricity_error"):
                attributes["endpoint_error"] = data["consumption_electricity_error"]
            return attributes
        if self._key.startswith(("curve_", "latest_band_", "latest_curve_")) or self._key == "latest_peak_power":
            latest_curve = _latest(curves)
            keys = (
                "dataCompetenzaStart", "dataCompetenzaEnd", "fascia1", "fascia2",
                "fascia3", "curveconsumomensile", "piccopotenzamensile",
            )
            return {
                "latest_curve": {key: latest_curve.get(key) for key in keys if latest_curve.get(key) is not None},
                "curve_consumption_history": [
                    {key: item.get(key) for key in keys if item.get(key) is not None}
                    for item in curves
                ],
            }
        if self._key == "greeners_status":
            attributes = {key: value for key, value in data["greeners"].items() if key != "statusGreeners"}
            if data.get("greeners_error"):
                attributes["endpoint_error"] = data["greeners_error"]
            return attributes
        if self._key.startswith("consumption_") or self._key == "latest_consumption":
            latest = _latest(consumption)
            return {
                "latest_competence_start": latest.get("dataCompetenzaStart"),
                "latest_competence_end": latest.get("dataCompetenzaEnd"),
                "latest_issue_date": latest.get("dataEmissione"),
                "consumption_history": [
                    {
                        key: item.get(key)
                        for key in ("dataEmissione", "dataCompetenzaStart", "dataCompetenzaEnd", "consumo")
                        if item.get(key) is not None
                    }
                    for item in consumption
                ],
            }
        if self._key.startswith(("gas_", "electricity_")) or self._key in ("latest_gas_amount", "latest_electricity_amount"):
            commodity = "gas" if "gas" in self._key else "electricity"
            codes = ("gas",) if commodity == "gas" else ("ele", "electricity")
            selected = [bill for bill in bills if str(bill.get("commodity", "")).lower() in codes]
            return {
                "commodity": commodity,
                "bills": [
                    {
                        key: bill.get(key)
                        for key in (
                            "invoiceId", "commodity", "amount", "competenceStartDate",
                            "competenceEndDate", "dueDate", "issueDate", "statusCode",
                            "statusLabel", "remainingAmountDue", "paymentMethod",
                            "productName", "consumptionType",
                        )
                        if bill.get(key) is not None
                    }
                    for bill in selected
                ],
            }
        latest_bill = _latest_bill(bills)
        if self._key.startswith("latest_"):
            details = {
                "invoice_id": latest_bill.get("invoiceId"),
                "commodity": latest_bill.get("commodity"),
                "amount": latest_bill.get("amount"),
                "competence_start": latest_bill.get("competenceStartDate"),
                "competence_end": latest_bill.get("competenceEndDate"),
                "issue_date": latest_bill.get("issueDate"),
                "due_date": latest_bill.get("dueDate"),
                "status": latest_bill.get("statusLabel") or latest_bill.get("invoiceStatus"),
                "status_description": latest_bill.get("statusDescription"),
                "partial_amount_paid": latest_bill.get("partialAmountPaid"),
                "payment_method": latest_bill.get("paymentMethod"),
                "download_action": latest_bill.get("downloadCtaCode"),
                "product_name": latest_bill.get("productName"),
                "consumption_type": latest_bill.get("consumptionType"),
            }
            return {key: value for key, value in details.items() if value is not None}
        return {
            "bills": [
                {
                    key: bill.get(key)
                    for key in (
                        "invoiceId", "commodity", "amount", "competenceStartDate",
                        "competenceEndDate", "dueDate", "invoiceStatus", "issueDate",
                        "paymentMethod", "remainingAmountDue", "partialAmountPaid",
                        "statusCode", "statusLabel", "statusDescription", "productName",
                        "consumptionType", "downloadCtaCode",
                    )
                    if bill.get(key) is not None
                }
                for bill in bills
            ]
        }
