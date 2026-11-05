import pytest

from twquant.valuation import value_company


def test_manufacturing_dcf_discloses_three_ordered_scenarios_and_assumptions():
    result = value_company({"fcff": 1_000, "net_debt": 100, "shares_outstanding": 100,
                            "growth": .08, "wacc": .10, "terminal_growth": .02}, "semiconductor")
    assert result["status"] == "ok"
    assert result["method"] == "fcff_dcf"
    assert [s["name"] for s in result["scenarios"]] == ["conservative", "base", "optimistic"]
    values = [s["fair_value"] for s in result["scenarios"]]
    assert values[0] < values[1] < values[2]
    assert result["scenarios"][1]["assumptions"]["wacc"] == .10


def test_financial_uses_book_value_and_roe_instead_of_manufacturing_cash_flow():
    data = {"book_value_per_share": 30, "roe": .12, "cost_of_equity": .10, "terminal_growth": .02,
            "fcff": 1_000_000, "net_debt": 0, "shares_outstanding": 1,
            "growth": .30, "wacc": .10}
    result = value_company(data, "financial")
    assert result["status"] == "ok"
    assert result["method"] == "justified_pb"
    assert result["scenarios"][1]["fair_value"] == pytest.approx(37.5)


def test_missing_fundamentals_abstain_instead_of_inventing_numbers():
    result = value_company({"ticker": "2330"}, "semiconductor")
    assert result["status"] == "insufficient_data"
    assert result["scenarios"] == []
    assert "fcff" in result["reason"]


@pytest.mark.parametrize("invalid", [{"wacc": .01}, {"shares_outstanding": 0}, {"fcff": float("nan")}])
def test_invalid_dcf_inputs_are_not_presented_as_fair_value(invalid):
    data = {"fcff": 1_000, "net_debt": 100, "shares_outstanding": 100,
            "growth": .08, "wacc": .10, "terminal_growth": .02, **invalid}
    result = value_company(data, "electronics")
    assert result["status"] in {"invalid_inputs", "insufficient_data"}
    assert result["scenarios"] == []


def test_forward_pe_is_explicit_fallback_without_dcf_inputs():
    result = value_company({"eps": 10, "pe": 20}, "electronics")
    assert result["method"] == "forward_pe"
    assert result["scenarios"][1]["fair_value"] == 200
