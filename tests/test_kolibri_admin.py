from theaxiom import kolibri_admin, kolibri_native, update_discovery_config


def test_generic_channel_discovery():
    channel = "a"*32
    config = {"format":"kolibri","kolibri_channel_id":channel,"update_policy":"notify"}
    assert update_discovery_config.settings(config) == {
        "method":"kolibri", "endpoint":kolibri_native.metadata_url(channel), "periodic":True}
    assert not update_discovery_config.settings(dict(config, update_policy="disabled"))["periodic"]
    assert kolibri_admin.channel({"configuration":config}) == channel


def test_configured_publisher_base():
    import pytest
    cid = "b"*32
    config = {"format":"kolibri","kolibri_channel_id":cid,
              "source_url":"https://publisher.example.org/kolibri/content/databases/"+cid+".sqlite3"}
    assert kolibri_native.source_base(config) == "https://publisher.example.org/kolibri/content/"
    assert update_discovery_config.settings(config)["endpoint"] == config["source_url"]
    with pytest.raises(ValueError):
        kolibri_native.source_base(dict(config, source_url="http://publisher.example.org/content/databases/"+cid+".sqlite3"))
