"""Industry-specific valuation, independent of short-term forecasts.

All monetary inputs must use the same currency and scale; shares_outstanding
must use the corresponding actual share count. Growth/rates use decimals.
No company financial numbers or discount rates are invented when absent.
"""

from __future__ import annotations

import math


def _failure(status, method, reason):
    return {"status": status, "method": method, "scenarios": [], "reason": reason}


def _numbers(inputs, keys):
    missing = [key for key in keys if key not in inputs or inputs[key] is None or str(inputs[key]).strip() == ""]
    if missing:
        raise KeyError(", ".join(missing))
    result = {key: float(inputs[key]) for key in keys}
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("All valuation inputs must be finite")
    return result


def value_company(inputs: dict, sector: str) -> dict:
    """Return three disclosed scenarios or an explicit data/assumption refusal.

    Manufacturing uses five-year FCFF DCF when its complete inputs are present,
    and forward P/E only when explicitly supplied EPS and P/E are available.
    Financials use a stable-growth justified P/B model; a bank never falls back
    to FCFF. Scenario shocks are stated methodology, not fitted price forecasts.
    """
    financial = sector.lower() in {"financial", "finance", "bank", "banking", "insurance", "金融", "金融保險"}
    method = "justified_pb" if financial else "fcff_dcf"
    scenario_spec = [("conservative", -0.02, 0.01), ("base", 0.0, 0.0), ("optimistic", 0.02, -0.01)]
    try:
        if financial:
            values = _numbers(inputs, ["book_value_per_share", "roe", "cost_of_equity", "terminal_growth"])
            if values["book_value_per_share"] <= 0:
                raise ValueError("book_value_per_share must be positive")
            scenarios = []
            for name, growth_shock, risk_shock in scenario_spec:
                roe = values["roe"] + growth_shock
                cost = values["cost_of_equity"] + risk_shock
                growth = values["terminal_growth"]
                if not (-1 < growth < cost) or roe <= growth:
                    raise ValueError("Each scenario requires cost_of_equity > terminal_growth and ROE > terminal_growth")
                multiple = (roe - growth) / (cost - growth)
                assumptions = {**values, "roe": roe, "cost_of_equity": cost,
                               "justified_pb": multiple, "methodology": "stable growth; ROE constant in perpetuity"}
                scenarios.append({"name": name, "fair_value": values["book_value_per_share"] * multiple,
                                  "assumptions": assumptions})
        else:
            keys = ["fcff", "net_debt", "shares_outstanding", "growth", "wacc", "terminal_growth"]
            present = all(key in inputs and inputs[key] is not None and str(inputs[key]).strip() != "" for key in keys)
            if not present and all(key in inputs and inputs[key] is not None for key in ["eps", "pe"]):
                method = "forward_pe"
                values = _numbers(inputs, ["eps", "pe"])
                if min(values.values()) <= 0:
                    raise ValueError("Forward EPS and P/E must be positive")
                scenarios = [{"name": name, "fair_value": values["eps"] * values["pe"] * multiplier,
                              "assumptions": {"eps": values["eps"], "pe": values["pe"] * multiplier,
                                              "methodology": "P/E scenario multiple -20% / unchanged / +20%"}}
                             for name, multiplier in [("conservative", .8), ("base", 1.0), ("optimistic", 1.2)]]
            else:
                values = _numbers(inputs, keys)
                years = int(inputs.get("years", 5))
                if values["fcff"] <= 0 or values["shares_outstanding"] <= 0 or not 1 <= years <= 30:
                    raise ValueError("DCF requires positive FCFF/shares and years between 1 and 30")
                scenarios = []
                for name, growth_shock, risk_shock in scenario_spec:
                    growth = values["growth"] + growth_shock
                    discount = values["wacc"] + risk_shock
                    terminal = values["terminal_growth"]
                    if not (-1 < growth <= 1) or not (-1 < terminal < discount) or discount <= 0:
                        raise ValueError("Each scenario requires WACC > terminal_growth and valid growth rates")
                    cashflows = [values["fcff"] * (1 + growth) ** year for year in range(1, years + 1)]
                    present_value = sum(cash / (1 + discount) ** year for year, cash in enumerate(cashflows, 1))
                    terminal_value = cashflows[-1] * (1 + terminal) / (discount - terminal)
                    equity = present_value + terminal_value / (1 + discount) ** years - values["net_debt"]
                    assumptions = {**values, "growth": growth, "wacc": discount, "years": years,
                                   "methodology": "growth shock -2pp / 0 / +2pp; WACC shock +1pp / 0 / -1pp"}
                    scenarios.append({"name": name, "fair_value": max(0.0, equity / values["shares_outstanding"]),
                                      "assumptions": assumptions})
    except KeyError as exc:
        return _failure("insufficient_data", method, f"Missing required inputs: {exc.args[0]}")
    except (ValueError, TypeError, OverflowError) as exc:
        return _failure("invalid_inputs", method, str(exc))
    return {"status": "ok", "method": method, "scenarios": scenarios,
            "reason": "Assumption-based fundamental value; separate from short-term price forecasts"}
