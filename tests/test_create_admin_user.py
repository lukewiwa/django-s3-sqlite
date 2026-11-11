from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

pytestmark = pytest.mark.django_db


def run(*args):
    out = StringIO()
    call_command("create_admin_user", *args, stdout=out)
    return out.getvalue()


def test_default_admin_gets_a_random_password():
    output = run()
    password = output.split("password: ")[1].split()[0]
    user = get_user_model().objects.get(username="admin")
    assert user.is_superuser
    assert user.check_password(password)
    assert len(password) >= 16


def test_explicit_arguments():
    run("luke", "luke@example.com", "hunter2")
    assert get_user_model().objects.get(username="luke").check_password("hunter2")


def test_bad_arguments_explain_themselves():
    output = run("name", "x@example.com", "pw", "unexpected")
    assert "ERROR" in output
    assert "arguments expected" in output
    assert not get_user_model().objects.exists()
