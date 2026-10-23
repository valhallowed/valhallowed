import re

from vhd_control.naming import child_id


def test_child_ids_are_compose_names_that_never_collide() -> None:
    names = ["Meadowfold", "meadowfold", "Meadow fold", "Meadow_fold", "9 Worlds"]
    ids = [child_id(n) for n in names]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", i) for i in ids)
    assert child_id("Meadowfold") == child_id("Meadowfold")


def test_the_longest_save_name_still_makes_a_dns_label() -> None:
    longest = "A" + "b-" * 31 + "c"
    assert len(longest) == 64
    cid = child_id(longest)
    assert len(cid) <= 63
    assert re.fullmatch(r"world-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-[0-9a-f]{6}", cid)
    assert child_id(longest + "") != child_id(longest[:-1] + "d")
