import pytest

from config import load_app_config


def test_example_config_loads():
    cfg = load_app_config("config.example.yaml")
    assert cfg.enabled_stores == ["amazon"]
    assert cfg.posting.mode == "mixed"
    assert "amazon" in cfg.stores and cfg.stores["flipkart"].keep_params


def test_bad_values_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("enabled_stores: [nosuchstore]\n")
    with pytest.raises(ValueError):
        load_app_config(p)
    p.write_text("posting: {mode: yolo}\n")
    with pytest.raises(ValueError):
        load_app_config(p)


def test_custom_store_added(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("stores:\n  snapdeal: {name: Snapdeal, domains: [snapdeal.com]}\n"
                 "enabled_stores: [amazon, snapdeal]\n")
    cfg = load_app_config(p)
    assert cfg.stores["snapdeal"].domains == ["snapdeal.com"] and "amazon" in cfg.stores


async def test_store_toggle_runtime(settings):
    assert settings.enabled_stores == ["amazon"]
    await settings.enable_store("flipkart")
    assert settings.enabled_stores == ["amazon", "flipkart"]
    await settings.disable_store("amazon")
    assert settings.enabled_stores == ["flipkart"]
    with pytest.raises(ValueError):
        await settings.enable_store("nope")


async def test_overrides_persist(settings, cfg, env, db):
    from config import Settings
    await settings.add_source("@newsrc")
    await settings.set("mode", "auto")
    s2 = Settings(cfg, env, db)
    await s2.load_overrides()
    assert s2.mode == "auto" and "@newsrc" in s2.sources
    await s2.remove_source("@newsrc")
    assert "@newsrc" not in s2.sources
