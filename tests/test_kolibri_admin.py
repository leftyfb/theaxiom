from theaxiom import kolibri_admin, kolibri_native, update_discovery_config


def test_generic_channel_discovery():
    channel = "a"*32
    config = {"format":"kolibri","kolibri_channel_id":channel,"update_policy":"notify"}
    assert update_discovery_config.settings(config) == {
        "method":"kolibri", "endpoint":kolibri_native.metadata_url(channel), "periodic":True}
    assert not update_discovery_config.settings(dict(config, update_policy="disabled"))["periodic"]
    assert kolibri_admin.channel({"configuration":config}) == channel
