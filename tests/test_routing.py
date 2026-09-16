from js8mail.routing import LinkEvidence, RouteAction, RouteEngine, TemporalGraph

NOW = 1_700_000_000_000


def link(
    source: str, destination: str, score: float, age_ms: int = 0, available: bool = True
) -> LinkEvidence:
    return LinkEvidence(source, destination, NOW - age_ms, score, available=available)


def test_discovers_three_hop_path_and_explains_weakest_link() -> None:
    graph = TemporalGraph()
    graph.add(link("OH3SPN", "SM5AAA", 0.95))
    graph.add(link("SM5AAA", "DL2BBB", 0.80))
    graph.add(link("DL2BBB", "G0XYZ", 0.90))
    plan = RouteEngine(graph).choose("OH3SPN", "G0XYZ", now_ms=NOW)
    assert plan.action == RouteAction.RELAY_NOW
    assert plan.path == ("OH3SPN", "SM5AAA", "DL2BBB", "G0XYZ")
    assert plan.weakest_link_score == 0.8
    assert "weakest link 0.80" in plan.explanation


def test_rejects_cycle_and_deferred_when_no_valid_path() -> None:
    graph = TemporalGraph()
    graph.add(link("A", "B", 0.9))
    graph.add(link("B", "A", 0.9))
    graph.add(link("B", "C", 0.9))
    plan = RouteEngine(graph).choose("A", "Z", now_ms=NOW)
    assert plan.action == RouteAction.DEFER
    assert plan.path == ("A",)


def test_attempted_path_is_penalized_but_can_be_reused_and_stale_evidence_decays() -> None:
    graph = TemporalGraph()
    graph.add(link("A", "B", 0.95))
    graph.add(link("B", "C", 0.95))
    graph.add(link("A", "D", 0.65))
    graph.add(link("D", "C", 0.65))
    fresh = RouteEngine(graph).choose("A", "C", now_ms=NOW)
    assert fresh.action == RouteAction.RELAY_NOW
    repeated = RouteEngine(graph).choose("A", "C", now_ms=NOW, attempted_paths={fresh.path})
    assert repeated.action == RouteAction.RELAY_NOW
    assert repeated.path == ("A", "D", "C")
    exhausted = RouteEngine(graph).choose(
        "A", "C", now_ms=NOW, attempted_paths={fresh.path, repeated.path}
    )
    assert exhausted.path == fresh.path
    assert "no untried" in exhausted.explanation

    stale_graph = TemporalGraph()
    stale_graph.add(link("A", "C", 0.9, age_ms=10 * 86_400_000))
    stale = RouteEngine(stale_graph, minimum_score=0.25).choose("A", "C", now_ms=NOW)
    assert stale.action == RouteAction.DEFER


def test_replans_from_last_proven_custodian_when_relay_disappears() -> None:
    graph = TemporalGraph()
    graph.add(link("ORIGIN", "RELAY", 0.95, available=False))
    graph.add(link("CUSTODIAN", "DEST", 0.9))
    plan = RouteEngine(graph).choose("CUSTODIAN", "DEST", now_ms=NOW)
    assert plan.path == ("CUSTODIAN", "DEST")
    assert plan.action == RouteAction.DIRECT


def test_default_route_planner_allows_long_acyclic_paths() -> None:
    graph = TemporalGraph()
    for left, right in zip("ABCDEFGHIJ", "BCDEFGHIJK"):
        graph.add(link(left, right, 0.9))
    plan = RouteEngine(graph).choose("A", "K", now_ms=NOW)
    assert plan.path == tuple("ABCDEFGHIJK")
