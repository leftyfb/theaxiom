"""Generic publication changes refresh the manifest without deployment actions."""
from unittest.mock import Mock, patch

from theaxiom import zim_refresh


def test_reconcile_empty_library_invalidates_without_starting_reader(tmp_path):
    target = tmp_path / "library.xml"
    with (patch.object(zim_refresh.zim_catalog, "render_xml", return_value=b"<library/>"),
          patch.object(zim_refresh.zim_catalog, "manifest_path", return_value=target),
          patch.object(zim_refresh.zim_catalog, "write_manifest") as write,
          patch.object(zim_refresh.zim_reader_probe, "acknowledge") as ack):
        assert zim_refresh.reconcile(lambda: [], attempts=1)
        write.assert_called_once_with([])
        ack.assert_not_called()


def test_reconcile_retries_after_kiwix_updates_library(tmp_path):
    target = tmp_path / "library.xml"
    target.write_bytes(b"<library><book id='ready'/></library>")
    ack = Mock(side_effect=[ValueError("not loaded"), None])
    with (patch.object(zim_refresh.zim_catalog, "render_xml", return_value=target.read_bytes()),
          patch.object(zim_refresh.zim_catalog, "manifest_path", return_value=target),
          patch.object(zim_refresh.zim_reader_probe, "acknowledge", ack),
          patch.object(zim_refresh.time, "sleep") as sleep):
        assert zim_refresh.reconcile(lambda: ["ready"], attempts=3, interval=0.01)
        assert ack.call_count == 2
        sleep.assert_called_once_with(0.01)


def test_reconcile_reader_missing_returns_pending_not_published(tmp_path):
    target = tmp_path / "library.xml"
    target.write_bytes(b"<library><book id='ready'/></library>")
    with (patch.object(zim_refresh.zim_catalog, "render_xml", return_value=target.read_bytes()),
          patch.object(zim_refresh.zim_catalog, "manifest_path", return_value=target),
          patch.object(zim_refresh.zim_reader_probe, "acknowledge", side_effect=OSError("absent")),
          patch.object(zim_refresh.time, "sleep")):
        assert not zim_refresh.reconcile(lambda: ["ready"], attempts=3, interval=0)


def test_failed_replacement_reader_ack_rolls_back_then_reconciles_old_manifest():
    from theaxiom import zim_jobs
    checks = Mock(side_effect=[False, True])
    rollback = Mock(return_value={"state": "completed"})
    loader = lambda: ["ready"]
    configuration = {"format": "zim", "source_url": "https://example.org/new.zim"}
    with (patch.object(zim_refresh, "reconcile", checks),
          patch.object(zim_jobs, "rollback_candidate", rollback)):
        result = zim_refresh.reconcile_activation(
            loader, source_id="ready", configuration=configuration,
            attempts=2, interval=0,
        )
    assert result is False
    rollback.assert_called_once_with(
        "ready", configuration,
        reason="Replacement reader verification failed; previous verified archive restored",
    )
    assert checks.call_count == 2
    assert checks.call_args_list[0].args == (loader,)
    assert checks.call_args_list[1].args == (loader,)


def test_successful_replacement_reader_ack_never_rolls_back():
    from theaxiom import zim_jobs
    with (patch.object(zim_refresh, "reconcile", return_value=True) as check,
          patch.object(zim_jobs, "rollback_candidate") as rollback):
        assert zim_refresh.reconcile_activation(
            lambda: ["ready"], source_id="ready",
            configuration={"format": "zim"}, attempts=1, interval=0)
    check.assert_called_once()
    rollback.assert_not_called()


def test_failed_ack_does_not_clobber_divergent_serving_record():
    from theaxiom import zim_jobs
    with (patch.object(zim_refresh, "reconcile", return_value=False) as check,
          patch.object(zim_jobs, "rollback_candidate",
                       side_effect=ValueError("Serving archive changed")) as rollback):
        assert not zim_refresh.reconcile_activation(
            lambda: ["ready"], source_id="ready",
            configuration={"format": "zim"}, attempts=1, interval=0)
    rollback.assert_called_once()
    # No second reconciliation can run after rollback was refused.
    check.assert_called_once()


def test_replacement_schedule_routes_to_guarded_recovery_thread():
    loader = lambda: ["ready"]
    config = {"format": "zim", "sha256": "a" * 64}
    with patch.object(zim_refresh, "reconcile_activation", return_value=True) as worker:
        thread = zim_refresh.schedule(
            loader, activated_source="ready", activated_configuration=config)
        thread.join(timeout=5)
        assert not thread.is_alive()
    worker.assert_called_once()
    assert worker.call_args.args == (loader,)
    assert worker.call_args.kwargs["source_id"] == "ready"
    assert worker.call_args.kwargs["configuration"] == config
