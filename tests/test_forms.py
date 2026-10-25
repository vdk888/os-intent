import copy

import pytest

from oi import forms


@pytest.mark.parametrize("name", ["A", "B", "C"])
def test_packaged_forms_validate(name):
    f = forms.load_form(name)
    assert forms.validate(f) == []
    assert "nothing" in f["options"]


def test_validator_catches_errors():
    f = forms.load_form("A")
    bad = copy.deepcopy(f)
    bad["options"]["yes"] = {"criteria": "one two three four five six seven eight nine"}
    del bad["options"]["nothing"]
    bad["question"]["type"] = "score"
    bad["candidates"]["k"] = 20
    errs = forms.validate(bad)
    joined = "\n".join(errs)
    for frag in ["nothing", "'yes' forbidden", "over 8 words", "primitive", "choice", "k in 8..15"]:
        assert frag in joined, frag


def test_router_picks_C_in_safari_B_in_mail_and_A_otherwise():
    assert forms.route({"app": "Safari"})["form_id"] == "safari.next_page"
    assert forms.route({"app": "Mail"})["form_id"] == "mail.next_action"     # W14
    for app in ["Code", None, "Chrome"]:
        assert forms.route({"app": app})["form_id"] == "generic.next_action"


def test_router_skips_retired():
    fs = forms.all_forms()
    for f in fs:
        if f["form_id"] == "safari.next_page":
            f["status"] = "retired"
    assert forms.route({"app": "Safari"}, fs)["form_id"] == "generic.next_action"
