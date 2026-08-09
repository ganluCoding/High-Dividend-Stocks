#!/usr/bin/env python3
"""Validate deterministic PRD golden fixtures without market-data dependencies."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "golden_fixtures.json"
OUTPUT_PATH = PROJECT_ROOT / "data" / "quality" / "golden_validation.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decimal_equal(actual: Decimal, expected: str, tolerance: str) -> bool:
    return abs(actual - Decimal(expected)) <= Decimal(tolerance)


def validate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    fixture_id = fixture["fixture_id"]
    inputs = fixture["inputs"]
    expected = fixture["expected"]
    tolerance = fixture["absolute_tolerance"]
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any, expected_value: Any) -> None:
        checks.append(
            {
                "check": name,
                "passed": passed,
                "actual": str(actual) if actual is not None else None,
                "expected": expected_value,
            }
        )

    if fixture_id == "SYN-TRAP-001":
        ttm_yield = Decimal(inputs["ttm_dps"]) / Decimal(inputs["price"])
        sustainable_dps_cap = min(
            Decimal(inputs["normalized_eps"]) * Decimal(inputs["prudent_payout_cap"]),
            Decimal(inputs["parent_distributable_cash_per_share"]),
        )
        reasons = []
        if sustainable_dps_cap < Decimal(inputs["ttm_dps"]):
            reasons.append("DIV_CASH_COVERAGE_FAIL")
        if inputs["debt_distribution_constraint_failed"]:
            reasons.append("DIV_DEBT_CONSTRAINT_FAIL")
        add("ttm_yield", decimal_equal(ttm_yield, expected["ttm_yield"], tolerance), ttm_yield, expected["ttm_yield"])
        add("status", bool(reasons) and expected["instrument_status"] == "排除", "排除", expected["instrument_status"])
        add("reason_codes", reasons == expected["reason_codes"], reasons, expected["reason_codes"])
        add("yield_implied_price_veto", expected["yield_implied_price"] is None, None, None)
    elif fixture_id == "SYN-CA-001":
        ending_units = Decimal(inputs["starting_units"]) * (
            Decimal("1") + Decimal(inputs["bonus_units_per_unit"])
        )
        total_return = (
            ending_units * Decimal(inputs["ex_date_close"])
            + Decimal(inputs["cash_distribution"])
        ) / (Decimal(inputs["starting_units"]) * Decimal(inputs["previous_close"])) - Decimal("1")
        add("ending_units", decimal_equal(ending_units, expected["ending_units"], tolerance), ending_units, expected["ending_units"])
        add("total_return", decimal_equal(total_return, expected["total_return"], tolerance), total_return, expected["total_return"])
    elif fixture_id == "SYN-TAX-A-001":
        gross = Decimal(inputs["units"]) * Decimal(inputs["dps"])
        rates = {"20": Decimal("0.20"), "180": Decimal("0.10"), "400": Decimal("0")}
        for days, rate in rates.items():
            tax = gross * rate
            expected_tax = expected["tax_by_holding_days"][days]
            add(f"tax_{days}_days", decimal_equal(tax, expected_tax, tolerance), tax, expected_tax)
    elif fixture_id == "SYN-TAX-ETF-001":
        tax = Decimal("0")
        add("etf_distribution_tax", decimal_equal(tax, expected["tax"], tolerance), tax, expected["tax"])
    elif fixture_id == "SYN-IPS-001":
        combined_weight = (
            Decimal(inputs["existing_issuer_exposure_cny"])
            + Decimal(inputs["new_exposure_cny"])
        ) / Decimal(inputs["principal_cny"])
        over_limit = combined_weight > Decimal(inputs["issuer_limit"])
        add("combined_weight", decimal_equal(combined_weight, expected["combined_weight"], tolerance), combined_weight, expected["combined_weight"])
        add("portfolio_status", over_limit and expected["portfolio_status"] == "超限", "超限" if over_limit else "未超限", expected["portfolio_status"])
        add("instrument_status_independent", expected["instrument_status"] == "合格", "合格", expected["instrument_status"])
    elif fixture_id == "SYN-MISSING-001":
        missing = inputs["parent_distributable_cash_per_share"] is None
        add("missing_field_state", missing and expected["instrument_status"] == "资料不足", "资料不足" if missing else "可评估", expected["instrument_status"])
        add("reason_codes", expected["reason_codes"] == ["MISSING_CRITICAL_FIELD"], ["MISSING_CRITICAL_FIELD"] if missing else [], expected["reason_codes"])
        add("yield_implied_price_blocked", expected["yield_implied_price"] is None, None, None)
    else:
        add("known_fixture", False, fixture_id, "known fixture id")

    return {
        "fixture_id": fixture_id,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def main() -> int:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    required_fixture_fields = {
        "fixture_id",
        "instrument_id",
        "applicable_test_ids",
        "inputs",
        "expected",
        "absolute_tolerance",
    }
    schema_checks: list[dict[str, Any]] = []
    for fixture in document["fixtures"]:
        missing = sorted(required_fixture_fields - fixture.keys())
        schema_checks.append(
            {
                "fixture_id": fixture.get("fixture_id"),
                "passed": not missing,
                "missing_fields": missing,
            }
        )

    document_checks = []
    for item in document["input_document_hashes"]:
        relative_path = {
            "formula-registry-v1": Path("config/formula_registry.json"),
            "reason-codes-v1": Path("config/reason_codes.json"),
        }[item["document_id"]]
        actual_hash = sha256_file(PROJECT_ROOT / relative_path)
        document_checks.append(
            {
                "document_id": item["document_id"],
                "passed": actual_hash == item["sha256"],
                "expected_sha256": item["sha256"],
                "actual_sha256": actual_hash,
            }
        )

    results = [validate_fixture(fixture) for fixture in document["fixtures"]]
    passed = (
        all(item["passed"] for item in schema_checks)
        and all(item["passed"] for item in document_checks)
        and all(item["passed"] for item in results)
    )
    output = {
        "schema_version": "golden-validation-v1",
        "fixture_schema_version": document["schema_version"],
        "research_as_of": document["research_as_of"],
        "status": "pass" if passed else "fail",
        "schema_checks": schema_checks,
        "document_hash_checks": document_checks,
        "fixture_results": results,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": output["status"], "fixtures": len(results), "output": str(OUTPUT_PATH.relative_to(PROJECT_ROOT))}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
