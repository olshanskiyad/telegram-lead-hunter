from config import Settings


def test_registration_closed_by_default_for_strangers():
    s = Settings(ADMIN_IDS="10,20")
    assert not s.SELF_REGISTRATION
    assert not s.can_register(30)
    assert s.can_register(10)


def test_self_registration_switch_opens_it_for_everyone():
    s = Settings(ADMIN_IDS="10", SELF_REGISTRATION=True)
    assert s.can_register(30)
