"""Logic tests for the user-initiated, approval-gated escalation state machine.

Flow: budget hit (notify only) → user requests elevation (approval) → steward decides
(on approve, ADD to the next override group). Uses fakes so no workspace is needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TIERS_CONFIG", "config/tiers.yaml")

from app.budget.config import load_config          # noqa: E402
from app.budget.escalation import Escalator          # noqa: E402

CFG = load_config("config/tiers.yaml")
G2, G3 = CFG.tiers[1].group, CFG.tiers[2].group


class FakeGroups:
    def __init__(self, member_of=None):
        self.adds, self.removes = [], []
        self._member_of = set(member_of or [])

    def add(self, principal, group):
        self.adds.append((principal, group)); self._member_of.add(group)

    def remove(self, principal, group):
        self.removes.append((principal, group)); self._member_of.discard(group)

    def is_member(self, group, principal):
        return group in self._member_of


class FakeSystables:
    def usage_breakdown(self, principal, services, window, price_map):
        return {"total_usd": 3.0, "total_in": 100, "total_out": 900, "calls": 5,
                "models": [{"model": "opus", "calls": 5, "in_tok": 100, "out_tok": 900, "usd": 3.0}]}


class FakeStore:
    def __init__(self):
        self.requests, self.entitlements, self.audits, self.notes = {}, {}, [], []
        self._open = False; self._n = 0

    def create_request(self, principal, frm, to, spend):
        self._n += 1; rid = f"req-{self._n}"; self._open = True
        self.requests[rid] = {"principal": principal, "from_tier": frm, "to_tier": to, "status": "pending"}
        return rid

    def get(self, rid):
        from app.budget.approvals import Approval
        r = self.requests.get(rid)
        return Approval(rid, r["principal"], r["from_tier"], r["to_tier"], r["status"], None, None) if r else None

    def decide(self, rid, approver, decision, reason):
        self.requests[rid]["status"] = decision; self._open = False

    def has_open_request(self, principal):
        return self._open

    def get_entitlement(self, principal):
        return self.entitlements.get(principal)

    def set_entitlement(self, principal, tier, state):
        self.entitlements[principal] = (tier, state)

    def audit(self, *a, **k):
        self.audits.append((a, k))


class FakeNotifier:
    def __init__(self): self.events = []
    def budget_hit(self, *a, **k): self.events.append("budget_hit")
    def request_submitted(self, *a, **k): self.events.append("request_submitted")
    def notify_approver(self, *a, **k): self.events.append("notify_approver")
    def decision(self, *a, **k): self.events.append(("decision", a[3]))


def _esc(member_of=None):
    g = FakeGroups(member_of)
    return Escalator(CFG, FakeSystables(), g, FakeStore(), FakeNotifier(), app_url="http://app")


def test_config_shape():
    assert [t.threshold_usd for t in CFG.tiers] == [1, 2, 4]
    assert CFG.tiers[0].group == ""                       # default tier, no override group
    assert CFG.tiers[2].escalation == "none"              # top tier


def test_current_tier_from_membership():
    assert _esc().current_tier("u@x").name == "tier-1"            # no groups -> default
    assert _esc([G2]).current_tier("u@x").name == "tier-2"
    assert _esc([G2, G3]).current_tier("u@x").name == "tier-3"    # highest applies


def test_budget_hit_notifies_no_elevation():
    e = _esc()
    out = e.record_budget_hit("u@x", "tier-1")
    assert out["tier"] == "tier-1" and out["ceiling_usd"] == 1
    assert e._g.adds == []                                # no elevation on a hit
    assert "budget_hit" in e._n.events
    assert e._store.entitlements["u@x"] == ("tier-1", "over_pending")


def test_request_elevation_opens_request_no_grant():
    e = _esc()
    r = e.request_elevation("u@x")
    assert r.action == "approval_requested" and r.to_tier == "tier-2"
    assert e._g.adds == []                                # nothing granted until approval
    assert "notify_approver" in e._n.events


def test_request_elevation_idempotent_when_pending():
    e = _esc()
    e.request_elevation("u@x")
    r2 = e.request_elevation("u@x")
    assert r2.action == "noop"                            # already pending


def test_top_tier_cannot_request():
    e = _esc([G2, G3])
    r = e.request_elevation("u@x")
    assert r.action == "at_top" and e._g.adds == []


def test_approval_adds_override_group_and_notifies():
    e = _esc()
    r = e.request_elevation("u@x")
    out = e.apply_decision(r.request_id, "steward@x", "approved")
    assert out.action == "escalated" and out.to_tier == "tier-2"
    assert e._g.adds == [("u@x", G2)]
    assert ("decision", "approved") in e._n.events

def test_denial_no_grant_and_notifies():
    e = _esc()
    r = e.request_elevation("u@x")
    out = e.apply_decision(r.request_id, "steward@x", "denied", reason="too soon")
    assert out.action == "denied" and e._g.adds == []
    assert ("decision", "denied") in e._n.events


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print("PASS", fn.__name__)
    print(f"\n{len(fns)} passed")
