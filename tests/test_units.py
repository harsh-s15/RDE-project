import pytest

from tester import rules
from tester.__main__ import load_route
from tester.masking import Masker


def test_route_full_and_until():
    assert [t["id"] for t in load_route()] == ["pay", "allocate", "ship", "deliver"]
    assert [t["id"] for t in load_route(until="ship")] == ["pay", "allocate", "ship"]


@pytest.mark.parametrize("transitions", [
    "  - {id: pay, from: CREATED, to: PAID}\n  - {id: ship, from: ALLOCATED, to: SHIPPED}",  # gap
    "  - {id: pay, from: CREATED, to: PAID}\n  - {id: cancel, from: CREATED, to: SHIPPED}",  # branch
])
def test_loader_rejects_non_chain(tmp_path, transitions):
    spec = tmp_path / "states.yaml"
    spec.write_text(f"states: [CREATED, PAID, SHIPPED]\ntransitions:\n{transitions}\n")
    with pytest.raises(ValueError):
        load_route(spec)


def test_mask_hides_pii_keeps_diagnostics():
    mask = Masker()
    mask.register("name", "Aarav Sharma")
    mask.register("email", "aarav@example.org")
    trace = ("2026-10-19T12:40:00.123+00:00 INFO order a3f9c2 created for Aarav Sharma <aarav@example.org> "
             "phone +91 9876543210 status PAID Authorization: Bearer eyJhbGciOi.abc.def other@mail.com")
    out = mask(trace)
    for secret in ["Aarav", "aarav@example.org", "9876543210", "eyJhbGciOi", "other@mail.com"]:
        assert secret not in out
    for kept in ["a3f9c2", "PAID", "2026-10-19T12:40:00.123+00:00", "<NAME_1>", "<EMAIL_1>"]:
        assert kept in out


def test_rules_lookup_ranks_allocation_rule_first():
    assert rules.lookup("how fast should allocation happen")[0].startswith("R2.")
