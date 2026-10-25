import copy

from oi import candidates, forms
from oi.moments import NOTHING

ALL = lambda o: True  # noqa: E731


def test_menu_cap_nothing_and_dedupe(model, moments):
    for name in ["A", "C"]:
        f = forms.load_form(name)
        for m in moments[::7]:
            menu = candidates.build(f, m, model, is_executable=ALL)
            keys = [o["key"] for o in menu]
            assert len(menu) <= 16 and keys[-1] == NOTHING and len(set(keys)) == len(keys)
            assert all(len(o["criteria"].split()) <= 8 for o in menu)


def test_form_C_only_urls_apps_and_stay(model, moments):
    f = forms.load_form("C")
    m = next(x for x in moments if x["context"]["token"] == "activate:Safari")
    menu = candidates.build(f, m, model, is_executable=ALL)
    assert {o["primitive"] for o in menu} <= {"url.open", "app.open", None}
    assert menu[-1]["criteria"] == "stay on this page"
    assert any(o["key"] == "open_url_example_org_prr_approuve" and o["criteria"] == "open prr approuve on example.org"
               for o in menu)
    assert not any(o["params"].get("app") == "Safari" for o in menu)  # no-op switch removed


def test_is_executable_filters_and_cap_16(model, moments):
    f = forms.load_form("A")
    m = moments[50]
    menu = candidates.build(f, m, model, is_executable=lambda o: o["primitive"] != "app.open")
    assert not any(o["primitive"] == "app.open" for o in menu) and menu[-1]["key"] == NOTHING
    big = copy.deepcopy(f)
    for i in range(30):
        big["options"][f"fixed{i}"] = {"criteria": f"do synthetic thing {i}", "primitive": "x.run"}
        big["candidates"]["fixed"].append(f"fixed{i}")
    menu = candidates.build(big, m, model, is_executable=ALL)
    assert len(menu) == 16 and menu[-1]["key"] == NOTHING


def test_without_model_only_fixed_and_nothing():
    f = forms.load_form("A")
    m = {"context": {"app": "Mail", "token": "activate:Mail"}, "history": []}
    assert [o["key"] for o in candidates.build(f, m, None)] == [NOTHING]


def test_opaque_keys_and_question(model, moments):
    f = forms.load_form("A")
    menu = candidates.build(f, moments[10], model, is_executable=ALL, opaque=True)
    assert [o["key"] for o in menu] == [chr(65 + i) for i in range(len(menu))]
    q = candidates.as_question(f, menu)
    assert q["type"] == "choice" and set(q["criteria"]) == {o["key"] for o in menu}


def test_default_is_executable_urls_ok():
    assert candidates.default_is_executable({"primitive": "url.open", "params": {"url": "x.org/a"}})
    assert not candidates.default_is_executable({"primitive": "app.open",
                                                 "params": {"app": "NoSuchSyntheticApp123"}})
