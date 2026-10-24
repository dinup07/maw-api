"""Paths, value expressions, templates and edge conditions."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from maw.engine.paths import render, resolve_path, resolve_value, to_text
from maw.engine.routing import evaluate, make_router
from maw.engine.state import RESET, append_list, merge_dicts, new_turn_input, sum_counts
from maw.schema.workflow import Condition, Edge

STATE = {
    "input": {"customer": {"id": 7, "tier": "gold"}},
    "messages": [HumanMessage("hi"), AIMessage("hello there")],
    "nodes": {
        "classify": {"category": "billing", "tags": ["a", "b"], "score": 0.92, "count": 3},
        "empty": {},
    },
    "output": {"category": "Billing "},
}


# §8.4 required cases ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("input.customer.id", 7),
        ("nodes.classify.category", "billing"),
        ("state.classify.category", "billing"),
        ("classify.category", "billing"),
        ("classify.tags.1", "b"),
        ("classify.tags.-1", "b"),
        ("messages.-1.content", "hello there"),
        ("nodes.missing.x", None),
        ("classify.category.length", 7),
        ("classify.tags.9", None),
        ("messages.0.__class__", None),
    ],
)
def test_resolve_path(path, expected):
    assert resolve_path(STATE, path) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("nodes.empty.x || nodes.classify.category", "billing"),
        ("nodes.empty.x || 'default'", "default"),
        ({"a": ["input.customer.tier", True]}, {"a": ["gold", True]}),
        ("{{ nodes.classify.tags }}", ["a", "b"]),
        ("Tier: {{ input.customer.tier }} ({{ nodes.classify.score }})", "Tier: gold (0.92)"),
        ("'literal text'", "literal text"),
        ('"double quoted"', "double quoted"),
        (3, 3),
        (None, None),
        (True, True),
    ],
)
def test_resolve_value(spec, expected):
    assert resolve_value(spec, STATE) == expected


def test_render_with_names():
    out = render("{{ q }} | {{ nodes.classify.tags }} | {{ nope }}", STATE, names={"q": "Q"})
    assert out == 'Q | ["a", "b"] | '


def test_names_take_precedence_and_support_paths():
    names = {"articles": [{"id": "KB-1"}], "classify": {"category": "from-names"}}
    assert resolve_value("{{ articles.0.id }}", STATE, names) == "KB-1"
    assert resolve_value("classify.category", STATE, names) == "from-names"


def test_to_text():
    assert to_text(None) == ""
    assert to_text("x") == "x"
    assert to_text(AIMessage("content")) == "content"
    assert to_text({"a": 1}) == '{"a": 1}'


# Operators ---------------------------------------------------------------------------------------------
def cond(field, operator, value=None):
    return Condition(field=field, operator=operator, value=value, target="x")


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (cond("output.category", "equals", "billing"), True),  # case/whitespace-insensitive
        (cond("output.category", "equals", "it"), False),
        (cond("output.category", "not_equals", "it"), True),
        (cond("output.category", "in", ["it", "BILLING"]), True),
        (cond("output.category", "in", "billing"), True),  # scalar wrapped in a list
        (cond("output.category", "not_in", ["it", "hr"]), True),
        (cond("output.category", "contains", "ILL"), True),
        (cond("classify.tags", "contains", "A"), True),
        (cond("classify.tags", "contains", "z"), False),
        (cond("classify", "contains", "score"), True),
        (cond("classify.count", "contains", 3), False),
        (cond("classify.score", "gt", 0.9), True),
        (cond("classify.score", "gte", 0.92), True),
        (cond("classify.count", "lt", "4"), True),
        (cond("classify.count", "lte", 2), False),
        (cond("nodes.missing", "gt", 1), False),
        (cond("classify.category", "gt", 1), False),
        (cond("classify.category", "exists"), True),
        (cond("nodes.missing", "exists"), False),
        (cond("nodes.missing", "not_exists"), True),
        (cond("classify.category", "matches", "^bil+"), True),
        (cond("nodes.missing", "matches", ".*"), False),
    ],
)
def test_operators(condition, expected):
    assert evaluate(condition, STATE) is expected


def test_router_first_match_wins_and_defaults():
    edge = Edge(
        source="classify",
        target="fallback",
        condition=[
            Condition(field="classify.category", value="billing", target="first"),
            Condition(field="classify.score", operator="gt", value=0.5, target=["second", "third"]),
        ],
    )
    route, destinations = make_router(edge)
    assert route(STATE) == ["first"]
    assert route({"nodes": {"classify": {"score": 0.9}}}) == ["second", "third"]
    assert route({}) == ["fallback"]
    assert destinations == sorted(["first", "second", "third", "fallback", "__end__"])


def test_router_without_default_goes_to_end():
    edge = Edge(source="a", condition=[Condition(field="nodes.a.ok", value=True, target="end")])
    route, destinations = make_router(edge)
    assert route({}) == ["__end__"]
    assert route({"nodes": {"a": {"ok": True}}}) == ["__end__"]
    assert destinations == ["__end__"]


# Reducers ----------------------------------------------------------------------------------------------
def test_reducers_and_reset():
    assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert merge_dicts({"a": 1}, {RESET: True}) == {}
    assert sum_counts({"a": 1}, {"a": 2, "b": 1}) == {"a": 3, "b": 1}
    assert sum_counts({"a": 1}, {RESET: True}) == {}
    assert append_list([1], [2]) == [1, 2]
    assert append_list([1], [RESET]) == []
    turn = new_turn_input({"q": 1})
    assert turn["messages"][0].content == '{"q": 1}'
    assert turn["output"] is None and "vars" not in turn
