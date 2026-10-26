import pytest

from app import create_app


def test_testing_config_is_used(app):
    assert app.config["TESTING"] is True
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"


def test_home_page_is_mongolian(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'lang="mn"' in html
    assert "Эрдэнэт Ажил" in html


def test_unknown_page_shows_mongolian_404(client):
    response = client.get("/baihgui-huudas")
    assert response.status_code == 404
    assert "Хуудас олдсонгүй" in response.get_data(as_text=True)


def test_production_requires_secret_key(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setattr("config.ProductionConfig.SECRET_KEY", None)
    with pytest.raises(RuntimeError):
        create_app("production")


def test_csrf_is_enabled_outside_tests():
    from config import DevelopmentConfig, ProductionConfig
    assert DevelopmentConfig.WTF_CSRF_ENABLED is True
    assert ProductionConfig.WTF_CSRF_ENABLED is True
