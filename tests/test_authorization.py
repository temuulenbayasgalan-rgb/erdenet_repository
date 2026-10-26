import pytest

from app.models import Role

DASHBOARDS = {
    Role.ADMIN: "/admin/",
    Role.WORKER: "/worker/",
    Role.EMPLOYER: "/employer/",
}


@pytest.mark.parametrize("url", DASHBOARDS.values())
def test_anonymous_user_is_sent_to_login(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login?next=")


@pytest.mark.parametrize("user_role", Role.ALL)
@pytest.mark.parametrize("page_role", Role.ALL)
def test_each_dashboard_only_allows_its_role(client, make_user, login, user_role, page_role):
    make_user(phone="99110001", role=user_role)
    login("99110001")
    response = client.get(DASHBOARDS[page_role])
    if user_role == page_role:
        assert response.status_code == 200
    else:
        assert response.status_code == 403
        assert "Хандах эрхгүй" in response.get_data(as_text=True)


def test_logout_requires_login(client):
    response = client.post("/auth/logout")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/auth/login")


def test_forged_session_user_id_is_ignored(client):
    with client.session_transaction() as session:
        session["_user_id"] = "not-a-number"
    assert client.get("/worker/").status_code == 302
